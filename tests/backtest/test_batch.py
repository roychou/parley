"""Offline tests for the batch-coalescing LLM adapter (fake batch client, no network)."""
import asyncio
from types import SimpleNamespace

import pytest

from src.backtest.batch import BatchLLM


# ---- fake client.messages.batches ----------------------------------------
class _FakeBatches:
    """Records each create() as one 'wave' and serves canned per-request results.

    result_for(params, custom_id) -> ("succeeded", message) | (status, None).
    Default: every request succeeds, echoing its custom_id back on the message.
    """

    def __init__(self, result_for=None):
        self.waves: list[list[dict]] = []          # the requests of each submitted batch
        self._store: dict[str, list] = {}          # batch_id -> [individual responses]
        self._result_for = result_for or (lambda params, cid: ("succeeded", _msg(cid)))
        self._n = 0

    async def create(self, *, requests):
        self.waves.append(list(requests))
        bid = f"batch-{self._n}"
        self._n += 1
        responses = []
        for r in requests:
            status, msg = self._result_for(r["params"], r["custom_id"])
            result = (
                SimpleNamespace(type="succeeded", message=msg)
                if status == "succeeded"
                else SimpleNamespace(type=status)
            )
            responses.append(SimpleNamespace(custom_id=r["custom_id"], result=result))
        self._store[bid] = responses
        return SimpleNamespace(id=bid, processing_status="ended", request_counts=len(requests))

    async def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status="ended", request_counts=0)

    async def results(self, batch_id):
        # Mirror the real AsyncBatches.results: a coroutine that resolves to an
        # async-iterable (AsyncJSONLDecoder), so callers must `await` then iterate.
        return _AsyncList(self._store[batch_id])


class _AsyncList:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item


def _msg(text):
    """A minimal stand-in for anthropic Message: a single text block."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], custom_id=None)


def _client(batches):
    return SimpleNamespace(messages=SimpleNamespace(batches=batches))


# ---- tests ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_calls_coalesce_into_one_batch():
    batches = _FakeBatches()
    llm = BatchLLM(_client(batches), poll_interval=0)

    # Fired together (the decide_all / scaffold-map pattern) -> one batch wave.
    results = await asyncio.gather(*(
        llm.create(model="m", max_tokens=1, messages=[{"role": "user", "content": str(i)}])
        for i in range(5)
    ))
    assert len(batches.waves) == 1
    assert len(batches.waves[0]) == 5
    # Each caller gets its own response back (routed by custom_id).
    assert [r.content[0].text for r in results] == [f"req-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_dependency_chain_forms_successive_waves():
    batches = _FakeBatches()
    llm = BatchLLM(_client(batches), poll_interval=0)

    # First call resolves, THEN a second is issued from its result -> two waves
    # (mirrors the sentiment map -> reduce dependency).
    first = await llm.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "a"}])
    second = await llm.create(
        model="m", max_tokens=1, messages=[{"role": "user", "content": first.content[0].text}]
    )
    assert len(batches.waves) == 2
    assert second.content[0].text == "req-1"


@pytest.mark.asyncio
async def test_max_batch_flushes_early():
    batches = _FakeBatches()
    llm = BatchLLM(_client(batches), poll_interval=0, max_batch=3)

    await asyncio.gather(*(
        llm.create(model="m", max_tokens=1, messages=[{"role": "user", "content": str(i)}])
        for i in range(7)
    ))
    # 7 requests, cap 3 -> early flushes at 3 and 6, remainder on the debounce.
    assert [len(w) for w in batches.waves] == [3, 3, 1]


@pytest.mark.asyncio
async def test_failed_request_raises_for_that_caller_only():
    # custom_id req-1 errors; the rest succeed.
    def result_for(params, cid):
        return ("errored", None) if cid == "req-1" else ("succeeded", _msg(cid))

    batches = _FakeBatches(result_for=result_for)
    llm = BatchLLM(_client(batches), poll_interval=0)

    results = await asyncio.gather(*(
        llm.create(model="m", max_tokens=1, messages=[{"role": "user", "content": str(i)}])
        for i in range(3)
    ), return_exceptions=True)

    assert results[0].content[0].text == "req-0"
    assert isinstance(results[1], RuntimeError) and "errored" in str(results[1])
    assert results[2].content[0].text == "req-2"
