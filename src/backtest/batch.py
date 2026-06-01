"""
Batch-backed Messages adapter — coalesces concurrent LLM calls into Batch API jobs.

Why this exists:
The backtest fans out hard. `MultiAgentStrategy.decide_all` gathers every screened
candidate for a decision date at once, and each candidate's specialists fan out
again (fundamentals + technicals, plus the sentiment scaffold's map→reduce→synthesis).
At index scale that launches dozens of tickers × several calls *concurrently* — well
past tier-1 input-tokens-per-minute on both Haiku and Sonnet. The per-filing semaphore
in the scaffold bounds one filing's burst; it can't bound the aggregate across tickers.

`BatchLLM` mimics `client.messages` (same `.create(**params) -> Message` contract), so
it drops straight into the existing injection seam. But instead of firing each call
immediately it *debounce-coalesces* all calls that land in the same quiescent window
into a single Message Batches job:

- The Batch API has its own, far larger throughput allowance — no per-minute throttle.
- It's ~50% cheaper, and a backtest is latency-insensitive (the ideal batch workload).
- Dependency chains form successive waves automatically: the sentiment reduce call only
  happens after its map futures resolve, so each layer becomes its own batch.

Trade-off: batch jobs complete in (typically) seconds to minutes rather than inline, so
wall-clock per wave is higher — but nothing throttles, which is the whole point at scale.
See notes/release-2-or-3-candidates.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message

logger = logging.getLogger(__name__)


class BatchLLM:
    """Drop-in `client.messages` replacement that coalesces concurrent `.create`
    calls into Message Batches jobs.

    Coalescing is debounce-based: each call (re)arms a short timer, and when the
    event loop quiesces — i.e. every concurrently-gathered caller has enqueued its
    request and parked on its future — the timer fires and submits them as one batch.
    A size cap flushes early for very large waves.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        poll_interval: float = 3.0,
        debounce: float = 0.05,
        max_batch: int = 10_000,
    ):
        self._batches = client.messages.batches
        self._poll_interval = poll_interval
        self._debounce = debounce
        self._max_batch = max_batch
        self._pending: list[tuple[str, dict[str, Any], asyncio.Future[Message]]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._seq = 0

    async def create(self, **params: Any) -> Message:
        """Enqueue a Messages request; resolves when its batch wave completes."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Message] = loop.create_future()
        custom_id = f"req-{self._seq}"
        self._seq += 1
        self._pending.append((custom_id, params, fut))

        if len(self._pending) >= self._max_batch:
            self._flush()
        else:
            self._arm()
        return await fut

    # -- coalescing --------------------------------------------------------

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        loop = asyncio.get_event_loop()
        self._timer = loop.call_later(self._debounce, self._flush)

    def _flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._pending:
            return
        wave, self._pending = self._pending, []
        # Detach the job so the awaiting callers' event loop keeps turning.
        asyncio.ensure_future(self._run(wave))

    # -- one batch job -----------------------------------------------------

    async def _run(
        self, wave: list[tuple[str, dict[str, Any], asyncio.Future[Message]]]
    ) -> None:
        futures = {custom_id: fut for custom_id, _, fut in wave}
        try:
            requests = [{"custom_id": cid, "params": params} for cid, params, _ in wave]
            batch = await self._batches.create(requests=requests)
            logger.info(f"batch submitted id={batch.id} requests={len(wave)}")

            while batch.processing_status != "ended":
                await asyncio.sleep(self._poll_interval)
                batch = await self._batches.retrieve(batch.id)
            logger.info(f"batch ended id={batch.id} counts={batch.request_counts}")

            async for resp in await self._batches.results(batch.id):
                fut = futures.pop(resp.custom_id, None)
                if fut is None or fut.done():
                    continue
                if resp.result.type == "succeeded":
                    fut.set_result(resp.result.message)
                else:
                    fut.set_exception(
                        RuntimeError(f"batch request {resp.custom_id}: {resp.result.type}")
                    )
            # Any request without a returned result is a hard error for its caller.
            for cid, fut in futures.items():
                if not fut.done():
                    fut.set_exception(RuntimeError(f"batch request {cid}: no result returned"))
        except Exception as exc:  # noqa: BLE001 — propagate to every awaiting caller
            for fut in futures.values():
                if not fut.done():
                    fut.set_exception(exc)
