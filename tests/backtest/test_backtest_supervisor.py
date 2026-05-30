import pytest

import src.backtest.backtest_supervisor as bsup
from src.backtest.cache import SignalCache
from src.data.fundamentals import ValuationSnapshot, pe_band
from src.data.technicals import TechnicalsSnapshot
from src.schemas.fundamentals import FundamentalsAnalysis
from src.schemas.sentiment import SentimentAnalysis
from src.schemas.technicals import TechnicalsAnalysis

_REASONING = "Synthetic reasoning padded to satisfy the min_length=50 constraint on the signal."


def _val_snapshot(pe: float, report_date: str = "2025-07-30") -> ValuationSnapshot:
    return ValuationSnapshot(
        price_date="2026-01-09",
        report_date=report_date,
        period_end_date="2025-06-30",
        diluted_eps=10.0,
        profit_margin=0.20,
        rev_growth_yoy=0.10,
        debt_to_equity=0.5,
        pe_ratio=pe,
    )


def _tech_snapshot(as_of: str) -> TechnicalsSnapshot:
    return TechnicalsSnapshot(
        as_of=as_of,
        date_range={"start": "2025-01-01", "end": as_of},
        current_price=100.0,
        sma_20=98.0,
        rsi_14=55.0,
    )


def _fund_analysis() -> FundamentalsAnalysis:
    return FundamentalsAnalysis(
        specialist="fundamentals", ticker="AAA", signal="BULLISH", confidence=0.7,
        reasoning=_REASONING, as_of="2026-01-09", rev_growth_yoy=0.10, pe_ratio=18.0,
        profit_margin=0.20, debt_to_equity=0.5,
    )


def _tech_analysis() -> TechnicalsAnalysis:
    return TechnicalsAnalysis(
        specialist="technicals", ticker="AAA", signal="NEUTRAL", confidence=0.5,
        reasoning=_REASONING, as_of="2026-01-09", current_price=100.0, sma_20=98.0, rsi_14=55.0,
    )


def test_pe_band_thresholds():
    assert pe_band(None) == "na"
    assert pe_band(float("nan")) == "na"
    assert pe_band(-5.0) == "na"
    assert pe_band(12.0) == "low"
    assert pe_band(14.99) == "low"
    assert pe_band(15.0) == "fair"
    assert pe_band(40.0) == "fair"
    assert pe_band(40.01) == "high"
    assert pe_band(120.0) == "high"


@pytest.mark.asyncio
async def test_fundamentals_reused_same_band_technicals_recompute(monkeypatch, tmp_path):
    """The cost win: same filing + same P/E band => fundamentals computed once and
    reused across decision dates, while technicals (keyed by as_of) recompute each date."""
    fund_calls: list[str] = []
    tech_calls: list[str] = []

    async def fake_fundamentals(client, ticker, as_of, data):
        fund_calls.append(as_of)
        return _fund_analysis()

    async def fake_technicals(client, ticker, as_of, data):
        tech_calls.append(as_of)
        return _tech_analysis()

    monkeypatch.setattr(bsup, "_call_fundamentals_with_data", fake_fundamentals)
    monkeypatch.setattr(bsup, "_call_technicals_with_data", fake_technicals)

    cache = SignalCache(tmp_path)

    # Two decision dates: same filing, P/E 37 then 39 — both in the "fair" band.
    def fundamentals_loader_day1(t, d): return _val_snapshot(37.0)
    def fundamentals_loader_day2(t, d): return _val_snapshot(39.0)
    def technicals_loader(t, d): return _tech_snapshot(d)

    await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-09",
        fundamentals_loader=fundamentals_loader_day1,
        technicals_loader=technicals_loader,
        signal_cache=cache,
    )
    await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-16",
        fundamentals_loader=fundamentals_loader_day2,
        technicals_loader=technicals_loader,
        signal_cache=cache,
    )

    # Fundamentals: one LLM call (day 2 hit the cache — same filing, same band).
    assert fund_calls == ["2026-01-09"]
    # Technicals: one call per date (keyed by as_of).
    assert tech_calls == ["2026-01-09", "2026-01-16"]


@pytest.mark.asyncio
async def test_fundamentals_recompute_when_pe_crosses_band(monkeypatch, tmp_path):
    """When P/E crosses a band boundary the fundamentals signal is recomputed."""
    fund_calls: list[str] = []

    async def fake_fundamentals(client, ticker, as_of, data):
        fund_calls.append(as_of)
        return _fund_analysis()

    async def fake_technicals(client, ticker, as_of, data):
        return _tech_analysis()

    monkeypatch.setattr(bsup, "_call_fundamentals_with_data", fake_fundamentals)
    monkeypatch.setattr(bsup, "_call_technicals_with_data", fake_technicals)

    cache = SignalCache(tmp_path)
    technicals_loader = lambda t, d: _tech_snapshot(d)  # noqa: E731

    # P/E 39 ("fair") then 41 ("high") — same filing, different band.
    await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-09",
        fundamentals_loader=lambda t, d: _val_snapshot(39.0),
        technicals_loader=technicals_loader, signal_cache=cache,
    )
    await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-16",
        fundamentals_loader=lambda t, d: _val_snapshot(41.0),
        technicals_loader=technicals_loader, signal_cache=cache,
    )

    # Two computes: the band crossing invalidated the cache.
    assert fund_calls == ["2026-01-09", "2026-01-16"]


# ==========================================
# sentiment specialist integration (include_sentiment)
# ==========================================


def _patch_specialist_calls(monkeypatch):
    async def fake_fundamentals(client, ticker, as_of, data):
        return _fund_analysis()

    async def fake_technicals(client, ticker, as_of, data):
        return _tech_analysis()

    monkeypatch.setattr(bsup, "_call_fundamentals_with_data", fake_fundamentals)
    monkeypatch.setattr(bsup, "_call_technicals_with_data", fake_technicals)


@pytest.mark.asyncio
async def test_sentiment_included_when_filing_exists(monkeypatch, tmp_path):
    _patch_specialist_calls(monkeypatch)
    sentiment_calls: list[str] = []

    async def fake_sentiment(messages_api, ticker, as_of, config=None):
        sentiment_calls.append(ticker)
        return SentimentAnalysis(
            specialist="sentiment", ticker=ticker, signal="BEARISH", confidence=0.6,
            reasoning=_REASONING, as_of=as_of, tone="cautious",
            notable_changes=["new risk"], source_form="10-Q", filed="2026-01-05",
        )

    monkeypatch.setattr(bsup, "current_filing_key", lambda t, a: "ACC1")
    monkeypatch.setattr(bsup, "run_sentiment_specialist", fake_sentiment)

    decision = await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-09",
        fundamentals_loader=lambda t, d: _val_snapshot(20.0),
        technicals_loader=lambda t, d: _tech_snapshot(d),
        signal_cache=SignalCache(tmp_path), include_sentiment=True,
    )
    specialists = {s.specialist for s in decision.contributing_signals}
    assert specialists == {"fundamentals", "technicals", "sentiment"}
    assert sentiment_calls == ["AAA"]


@pytest.mark.asyncio
async def test_sentiment_skipped_when_no_filing(monkeypatch, tmp_path):
    _patch_specialist_calls(monkeypatch)
    monkeypatch.setattr(bsup, "current_filing_key", lambda t, a: None)  # e.g. delisted

    decision = await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-09",
        fundamentals_loader=lambda t, d: _val_snapshot(20.0),
        technicals_loader=lambda t, d: _tech_snapshot(d),
        signal_cache=SignalCache(tmp_path), include_sentiment=True,
    )
    # No filing -> sentiment dropped, just the two numeric specialists.
    assert {s.specialist for s in decision.contributing_signals} == {"fundamentals", "technicals"}


@pytest.mark.asyncio
async def test_sentiment_off_by_default(monkeypatch, tmp_path):
    _patch_specialist_calls(monkeypatch)

    # current_filing_key shouldn't even be consulted when include_sentiment is False.
    def _boom(t, a):
        raise AssertionError("current_filing_key consulted with sentiment off")

    monkeypatch.setattr(bsup, "current_filing_key", _boom)

    decision = await bsup.run_backtest_supervisor(
        client=None, ticker="AAA", as_of="2026-01-09",
        fundamentals_loader=lambda t, d: _val_snapshot(20.0),
        technicals_loader=lambda t, d: _tech_snapshot(d),
        signal_cache=SignalCache(tmp_path),
    )
    assert {s.specialist for s in decision.contributing_signals} == {"fundamentals", "technicals"}
