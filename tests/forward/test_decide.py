"""Forward decision provider: specialist composition incl. forward-only news."""
import pytest

import src.forward.decide as decide
from src.data.fundamentals import ValuationSnapshot
from src.data.technicals import TechnicalsSnapshot
from src.schemas.fundamentals import FundamentalsAnalysis
from src.schemas.news import NewsAnalysis
from src.schemas.sentiment import SentimentAnalysis
from src.schemas.technicals import TechnicalsAnalysis

_R = "Synthetic reasoning padded to satisfy the min_length=50 constraint on the signal."


def _val(t, d):
    return ValuationSnapshot(
        price_date=d, report_date="2025-07-30", period_end_date="2025-06-30",
        diluted_eps=10.0, profit_margin=0.2, rev_growth_yoy=0.1, debt_to_equity=0.5, pe_ratio=20.0,
    )


def _tech(t, d):
    return TechnicalsSnapshot(as_of=d, date_range={"start": "2026-01-01", "end": d},
                              current_price=100.0, sma_20=98.0, rsi_14=55.0)


def _patch_core(monkeypatch):
    async def fake_fund(mc, ticker, as_of, data):
        return FundamentalsAnalysis(specialist="fundamentals", ticker=ticker, signal="BULLISH",
                                    confidence=0.7, reasoning=_R, as_of=as_of, rev_growth_yoy=0.1,
                                    pe_ratio=20.0, profit_margin=0.2, debt_to_equity=0.5)

    async def fake_tech(mc, ticker, as_of, data):
        return TechnicalsAnalysis(specialist="technicals", ticker=ticker, signal="NEUTRAL",
                                  confidence=0.5, reasoning=_R, as_of=as_of, current_price=100.0,
                                  sma_20=98.0, rsi_14=55.0)

    monkeypatch.setattr(decide, "_call_fundamentals_with_data", fake_fund)
    monkeypatch.setattr(decide, "_call_technicals_with_data", fake_tech)


def _stub_news_source(t, a, lb):
    return [{"title": "headline", "summary": "s", "published": a}]


@pytest.mark.asyncio
async def test_all_four_specialists_when_available(monkeypatch):
    _patch_core(monkeypatch)
    monkeypatch.setattr(decide, "current_filing_key", lambda t, a: "ACC")

    async def fake_sentiment(mc, t, a, config=None, summary_cache=None):
        return SentimentAnalysis(specialist="sentiment", ticker=t, signal="BULLISH", confidence=0.6,
                                 reasoning=_R, as_of=a, tone="up")

    async def fake_news(mc, t, a, src, lookback_days=7, config=None):
        return NewsAnalysis(specialist="news", ticker=t, signal="BULLISH", confidence=0.8,
                            reasoning=_R, as_of=a, overall_tone="positive", n_articles=1)

    monkeypatch.setattr(decide, "run_sentiment_specialist", fake_sentiment)
    monkeypatch.setattr(decide, "run_news_specialist", fake_news)

    d = await decide.run_forward_decision(
        None, "AAA", "2026-05-22",
        fundamentals_loader=_val, technicals_loader=_tech,
        news_source=_stub_news_source, include_news=True,
    )
    assert {s.specialist for s in d.contributing_signals} == {
        "fundamentals", "technicals", "sentiment", "news"}


@pytest.mark.asyncio
async def test_news_ablation_off_drops_news(monkeypatch):
    _patch_core(monkeypatch)
    # no filing -> no sentiment, isolating the news-ablation effect
    monkeypatch.setattr(decide, "current_filing_key", lambda t, a: None)

    d = await decide.run_forward_decision(
        None, "AAA", "2026-05-22",
        fundamentals_loader=_val, technicals_loader=_tech,
        news_source=_stub_news_source, include_news=False,  # ablation: news off
    )
    assert {s.specialist for s in d.contributing_signals} == {"fundamentals", "technicals"}


@pytest.mark.asyncio
async def test_none_when_core_data_missing(monkeypatch):
    _patch_core(monkeypatch)
    d = await decide.run_forward_decision(
        None, "AAA", "2026-05-22",
        fundamentals_loader=lambda t, a: None,  # no fundamentals
        technicals_loader=_tech,
    )
    assert d is None


@pytest.mark.asyncio
async def test_news_none_is_dropped_not_fatal(monkeypatch):
    _patch_core(monkeypatch)
    monkeypatch.setattr(decide, "current_filing_key", lambda t, a: None)

    async def no_news(mc, t, a, src, lookback_days=7, config=None):
        return None  # source returned nothing

    monkeypatch.setattr(decide, "run_news_specialist", no_news)
    d = await decide.run_forward_decision(
        None, "AAA", "2026-05-22",
        fundamentals_loader=_val, technicals_loader=_tech,
        news_source=_stub_news_source, include_news=True,
    )
    assert {s.specialist for s in d.contributing_signals} == {"fundamentals", "technicals"}
