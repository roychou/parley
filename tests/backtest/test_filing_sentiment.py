import json

from src.backtest.filing_sentiment import (
    SentimentScoreCache,
    _changed_text,
    _split_sentences,
)


def test_split_sentences_drops_fragments():
    text = "Demand remained strong this quarter. Ok. Margins expanded on better mix."
    sents = _split_sentences(text)
    # "Ok." is < 20 chars -> dropped; the two substantive sentences remain
    assert len(sents) == 2
    assert all(len(s) > 20 for s in sents)


def test_changed_text_isolates_added_sentences():
    prior = ("Demand remained strong across all segments this quarter. "
             "We expect continued growth into next year.")
    current = ("Demand remained strong across all segments this quarter. "
               "We now expect demand to soften materially in the coming quarters.")
    changed = _changed_text(current, prior)
    # the unchanged first sentence is excluded; the new guidance sentence is captured
    assert "soften materially" in changed
    assert "continued growth" not in changed
    assert "Demand remained strong across all segments" not in changed


def test_changed_text_no_prior_returns_current_head():
    current = "We expect demand to soften materially in the coming quarters. " * 3
    changed = _changed_text(current, None)
    assert "soften materially" in changed


def test_changed_text_identical_filings_is_empty():
    text = "Demand remained strong across all segments this quarter overall."
    assert _changed_text(text, text).strip() == ""


def test_sentiment_score_cache_roundtrip(tmp_path):
    cache = SentimentScoreCache(root_dir=tmp_path, version="vtest")
    assert cache.get("000123") is None
    cache.set("000123", 0.42, "raised guidance")
    assert cache.get("000123") == 0.42
    # stored on disk as the documented shape
    stored = json.loads((tmp_path / "vtest" / "000123.json").read_text())
    assert stored["tone"] == 0.42 and stored["rationale"] == "raised guidance"


def test_cache_clamps_read_to_float(tmp_path):
    cache = SentimentScoreCache(root_dir=tmp_path, version="vtest")
    cache.set("abc", -1.0)
    assert cache.get("abc") == -1.0
