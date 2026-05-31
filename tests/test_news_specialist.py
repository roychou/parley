"""Offline tests for the news specialist (stub news source + stubbed LLM, no network)."""
import pytest

import src.agents.news_specialist as ns
from src.schemas.news import NewsAnalysis

_ARTICLES = [
    {"title": "Acme beats Q1, raises guidance", "summary": "Revenue up 20%, FY guide raised.",
     "published": "2026-05-18"},
    {"title": "Acme announces $2B buyback", "summary": "Board authorizes repurchase.",
     "published": "2026-05-20"},
]


class _Usage:
    input_tokens = output_tokens = 1


class _Block:
    def __init__(self, type, text=None, name=None, input=None):
        self.type, self.text, self.name, self.input = type, text, name, input


class _Resp:
    def __init__(self, content):
        self.content, self.usage = content, _Usage()


class _FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        if "tools" in kw:  # the structured synthesis call
            return _Resp([_Block("tool_use", name="submit_news", input={
                "specialist": "news",
                "signal": "BULLISH",
                "confidence": 0.7,
                "reasoning": "Beat-and-raise plus a sizable buyback are concrete bullish signals.",
                "overall_tone": "positive",
                "key_events": ["Q1 beat + raised guidance", "$2B buyback"],
            })])
        return _Resp([_Block("text", text="news summary")])  # scaffold call


@pytest.mark.asyncio
async def test_returns_news_analysis_with_metadata_filled():
    mc = _FakeMessages()

    def source(ticker, as_of, lookback):
        return _ARTICLES

    result = await ns.run_news_specialist(mc, "ACME", "2026-05-22", source, lookback_days=7)

    assert isinstance(result, NewsAnalysis)
    assert result.signal == "BULLISH"
    assert result.ticker == "ACME" and result.as_of == "2026-05-22"
    # metadata defaulted from the call, not trusted from the model
    assert result.n_articles == 2 and result.lookback_days == 7
    assert result.top_headlines and "buyback" in " ".join(result.top_headlines).lower()
    # 1 scaffold summary call (small blob -> single call) + 1 synthesis call
    assert sum("tools" in c for c in mc.calls) == 1


@pytest.mark.asyncio
async def test_returns_none_when_no_news():
    mc = _FakeMessages()

    def empty_source(ticker, as_of, lookback):
        return []

    assert await ns.run_news_specialist(mc, "ACME", "2026-05-22", empty_source) is None
    assert mc.calls == []  # no LLM calls when there's nothing to analyze


def test_format_articles_includes_dates_and_titles():
    blob = ns._format_articles(_ARTICLES)
    assert "2026-05-18" in blob and "raises guidance" in blob and "buyback" in blob


def test_combine_news_sources_dedupes_merges_and_sorts():
    def benzinga(t, a, lb):
        return [{"title": "Acme beats Q1", "published": "2026-05-18"},
                {"title": "Acme buyback", "published": "2026-05-20"}]

    def cnbc_rss(t, a, lb):
        return [{"title": "Acme buyback", "published": "2026-05-20"},   # dup of benzinga
                {"title": "Acme CEO interview", "published": "2026-05-21"}]

    merged = ns.combine_news_sources(benzinga, cnbc_rss)
    arts = merged("ACME", "2026-05-22", 7)
    titles = [a["title"] for a in arts]
    # newest-first, deduped
    assert titles == ["Acme CEO interview", "Acme buyback", "Acme beats Q1"]


def test_combine_news_sources_tolerates_a_failing_source():
    def good(t, a, lb):
        return [{"title": "Real headline", "published": "2026-05-20"}]

    def broken(t, a, lb):
        raise RuntimeError("feed down")

    merged = ns.combine_news_sources(broken, good)
    assert [a["title"] for a in merged("ACME", "2026-05-22", 7)] == ["Real headline"]
