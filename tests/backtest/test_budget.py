"""LLM spend cap: meter + wrapper + abort behavior."""
from types import SimpleNamespace

import pytest

from src.backtest.budget import BudgetedMessages, BudgetExceededError, BudgetMeter


def _resp(input_tokens, output_tokens):
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[],
    )


def test_meter_accumulates_and_trips():
    m = BudgetMeter(max_usd=1.0)
    # sonnet: $3/Mtok in, $15/Mtok out. 100k in + 10k out = 0.30 + 0.15 = $0.45
    m.charge("claude-sonnet-4-6", 100_000, 10_000)
    assert m.spent == pytest.approx(0.45)
    m.charge("claude-sonnet-4-6", 100_000, 10_000)  # 0.90 total — still under
    assert m.spent == pytest.approx(0.90)
    with pytest.raises(BudgetExceededError, match="budget cap"):
        m.charge("claude-sonnet-4-6", 100_000, 10_000)  # 1.35 > 1.0 -> trip


def test_meter_batch_discount_halves_cost():
    m = BudgetMeter(max_usd=None, batch_discount=0.5)
    m.charge("claude-sonnet-4-6", 100_000, 10_000)
    assert m.spent == pytest.approx(0.225)  # half of 0.45


def test_meter_none_cap_never_trips():
    m = BudgetMeter(max_usd=None)
    for _ in range(1000):
        m.charge("claude-haiku-4-5-20251001", 100_000, 10_000)
    assert m.calls == 1000  # no exception


@pytest.mark.asyncio
async def test_budgeted_messages_meters_then_delegates():
    calls = []

    class _Inner:
        async def create(self, **kw):
            calls.append(kw)
            return _resp(50_000, 5_000)

    meter = BudgetMeter(max_usd=1.0)
    bm = BudgetedMessages(_Inner(), meter)
    await bm.create(model="claude-sonnet-4-6", max_tokens=10, messages=[])
    # 50k in + 5k out sonnet = 0.15 + 0.075 = 0.225
    assert meter.spent == pytest.approx(0.225)
    assert meter.calls == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_budgeted_messages_raises_when_over_cap():
    class _Inner:
        async def create(self, **kw):
            return _resp(1_000_000, 100_000)  # ~$4.5 sonnet, well over a $1 cap

    bm = BudgetedMessages(_Inner(), BudgetMeter(max_usd=1.0))
    with pytest.raises(BudgetExceededError):
        await bm.create(model="claude-sonnet-4-6", max_tokens=10, messages=[])
