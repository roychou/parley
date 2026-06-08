"""Hybrid (carry-forward) sentiment — orchestration tests with a stubbed LLM + EDGAR."""
import json

import pytest

import src.backtest.filing_sentiment as fs
from src.agents.sentiment_specialist import FilingSummaryCache
from src.schemas.sentiment import SentimentAnalysis


class _FakeMessages:
    """Records the user prompt it was called with, returns a canned submit_sentiment."""

    def __init__(self, signal="BULLISH", confidence=0.7):
        self.signal = signal
        self.confidence = confidence
        self.last_prompt = None

    async def create(self, **params):
        self.last_prompt = params["messages"][0]["content"]

        class _Block:
            type = "tool_use"
            name = "submit_sentiment"
            input = {
                "specialist": "sentiment", "signal": self.signal,
                "confidence": self.confidence, "reasoning": "x" * 60,
                "tone": "constructive and improving", "key_themes": ["demand up", "margins up"],
                "notable_changes": ["raised guidance"],
            }

        class _Usage:
            input_tokens = 100
            output_tokens = 50

        class _Resp:
            content = [_Block()]
            usage = _Usage()

        return _Resp()


def _patch_filings(monkeypatch, current, prior):
    monkeypatch.setattr(fs, "_select_filings", lambda t, a: (current, prior))
    monkeypatch.setattr(fs, "_section_text",
                        lambda t, f: f"section text for {f['accession']}")


_CUR = {"accession": "CUR1", "form": "10-Q", "filed": "2025-05-01", "primary_document": "x.htm"}
_PRIOR = {"accession": "PRIOR1", "form": "10-Q", "filed": "2025-02-01", "primary_document": "y.htm"}


@pytest.mark.asyncio
async def test_hybrid_bootstrap_when_no_carried_memo(monkeypatch, tmp_path):
    _patch_filings(monkeypatch, _CUR, _PRIOR)
    mc = _FakeMessages()
    cache = FilingSummaryCache(tmp_path, version="t")
    res = await fs.run_sentiment_hybrid(mc, "AAA", "2025-05-15", tone_cache=cache)
    # bootstrap path: no carried memo for the prior -> prompt uses the full current narrative
    assert isinstance(res, SentimentAnalysis)
    assert "bootstrap baseline" in mc.last_prompt
    assert cache.get("CUR1") is not None  # memo seeded for next quarter


@pytest.mark.asyncio
async def test_hybrid_uses_carried_memo_and_diff(monkeypatch, tmp_path):
    _patch_filings(monkeypatch, _CUR, _PRIOR)
    mc = _FakeMessages()
    cache = FilingSummaryCache(tmp_path, version="t")
    cache.set("PRIOR1", "[NEUTRAL@0.50] steady tone Themes: baseline")  # prior carried memo exists
    res = await fs.run_sentiment_hybrid(mc, "AAA", "2025-05-15", tone_cache=cache)
    assert isinstance(res, SentimentAnalysis)
    # incremental path: prompt carries the standing tone forward + the diff
    assert "STANDING TONE (carried from prior" in mc.last_prompt
    assert "WHAT CHANGED" in mc.last_prompt
    # and it writes an updated memo for the CURRENT accession (next quarter's prior)
    assert cache.get("CUR1") is not None
    assert "BULLISH@0.70" in cache.get("CUR1")


@pytest.mark.asyncio
async def test_hybrid_none_when_no_filing(monkeypatch, tmp_path):
    _patch_filings(monkeypatch, None, None)
    res = await fs.run_sentiment_hybrid(_FakeMessages(), "AAA", "2025-05-15",
                                        tone_cache=FilingSummaryCache(tmp_path, version="t"))
    assert res is None


@pytest.mark.asyncio
async def test_hybrid_carried_memo_roundtrips_to_next_quarter(monkeypatch, tmp_path):
    """The memo written this quarter is the standing tone read next quarter."""
    cache = FilingSummaryCache(tmp_path, version="t")
    # Q1: bootstrap, writes memo for CUR1
    _patch_filings(monkeypatch, _CUR, None)
    await fs.run_sentiment_hybrid(_FakeMessages("BULLISH", 0.8), "AAA", "2025-05-15",
                                  tone_cache=cache)
    assert "BULLISH@0.80" in cache.get("CUR1")
    # Q2: new filing CUR2 whose prior is CUR1 -> must read CUR1's memo as standing tone
    cur2 = {"accession": "CUR2", "form": "10-Q", "filed": "2025-08-01", "primary_document": "z.htm"}
    _patch_filings(monkeypatch, cur2, _CUR)
    mc2 = _FakeMessages("BEARISH", 0.6)
    await fs.run_sentiment_hybrid(mc2, "AAA", "2025-08-15", tone_cache=cache)
    assert "BULLISH@0.80" in mc2.last_prompt  # carried Q1 tone surfaced as Q2 standing tone


def test_carried_memo_format():
    a = SentimentAnalysis(specialist="sentiment", ticker="AAA", signal="BEARISH",
                          confidence=0.42, reasoning="x" * 60, as_of="2025-05-15",
                          tone="cautious", key_themes=["a", "b"])
    memo = fs._carried_memo(a)
    assert "BEARISH@0.42" in memo and "cautious" in memo and "a; b" in memo
