"""Offline tests for the sentiment specialist (stubbed EDGAR + LLM — no network)."""
import pytest

import src.agents.sentiment_specialist as ss
from src.schemas.sentiment import SentimentAnalysis

_FILINGS = [
    {"form": "10-Q", "filed": "2026-04-29", "accession": "A2", "primary_document": "d2.htm"},
    {"form": "10-K", "filed": "2025-07-30", "accession": "AK", "primary_document": "k.htm"},
    {"form": "10-Q", "filed": "2026-01-28", "accession": "A1", "primary_document": "d1.htm"},
]


# ---- fake AsyncAnthropic ----
class _Usage:
    input_tokens = 1
    output_tokens = 1


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
            return _Resp([_Block("tool_use", name="submit_sentiment", input={
                "specialist": "sentiment",
                "signal": "BEARISH",
                "confidence": 0.7,
                "reasoning": "Tone turned cautious; a new AI-competition risk appeared vs prior.",
                "tone": "cautious",
                "notable_changes": ["new AI-competition risk"],
                "key_themes": ["cloud"],
            })])
        return _Resp([_Block("text", text="narrative summary")])  # scaffold calls


def _patch_edgar(monkeypatch):
    monkeypatch.setattr(ss, "recent_filings", lambda t, *a, **k: _FILINGS)
    monkeypatch.setattr(ss, "fetch_filing_document", lambda t, acc, doc: "<html>x</html>")
    monkeypatch.setattr(
        ss, "extract_sections",
        lambda text, form: {"mdna": "MD&A " * 50, "risk_factors": "Risk " * 50},
    )


def test_select_filings_current_and_prior_same_form(monkeypatch):
    monkeypatch.setattr(ss, "recent_filings", lambda t, *a, **k: _FILINGS)
    current, prior = ss._select_filings("MSFT", "2026-05-01")
    assert current["accession"] == "A2"          # latest filed <= as_of
    assert prior["accession"] == "A1"            # prior *10-Q* (skips the 10-K between)


def test_current_filing_key(monkeypatch):
    monkeypatch.setattr(ss, "recent_filings", lambda t, *a, **k: _FILINGS)
    assert ss.current_filing_key("MSFT", "2026-05-01") == "A2"
    assert ss.current_filing_key("MSFT", "2020-01-01") is None  # nothing filed yet


@pytest.mark.asyncio
async def test_run_returns_sentiment_analysis(monkeypatch):
    _patch_edgar(monkeypatch)
    mc = _FakeMessages()  # the injected MessageCreator (client.messages, or a BatchLLM)
    result = await ss.run_sentiment_specialist(mc, "MSFT", "2026-05-01")

    assert isinstance(result, SentimentAnalysis)
    assert result.signal == "BEARISH"
    assert result.ticker == "MSFT" and result.as_of == "2026-05-01"
    assert result.source_form == "10-Q" and result.filed == "2026-04-29"  # defaults filled
    assert result.notable_changes == ["new AI-competition risk"]
    # 2 filing summaries (current + prior) + 1 synthesis call:
    assert len(mc.calls) == 3
    assert sum("tools" in c for c in mc.calls) == 1


@pytest.mark.asyncio
async def test_run_returns_none_when_no_filing(monkeypatch):
    monkeypatch.setattr(ss, "recent_filings", lambda t, *a, **k: [])
    assert await ss.run_sentiment_specialist(_FakeMessages(), "MSFT", "2026-05-01") is None
