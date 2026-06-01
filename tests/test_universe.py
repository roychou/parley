"""Tests for the point-in-time Nasdaq-100 universe (N-PORT-derived).

Membership is stubbed (no network); the ticker resolver and as-of/range/end logic
are exercised directly. Live N-PORT parsing is validated by the build step.
"""
import src.data.universe as universe

# Synthetic membership, newest-first (the shape build_membership() produces). DDD
# was a member through 2024 then dropped; CCC was added 2025.
_MEM = [
    {"period": "2026-03-31", "accession": "x3", "tickers": ["AAA", "BBB", "CCC"]},
    {"period": "2025-12-31", "accession": "x2", "tickers": ["AAA", "BBB", "CCC"]},
    {"period": "2024-12-31", "accession": "x1", "tickers": ["AAA", "BBB", "DDD"]},
]


def _patch(monkeypatch):
    monkeypatch.setattr(universe, "_membership", lambda: _MEM)


# ---- ticker resolution -------------------------------------------------------


def test_resolve_prefers_cusip_override():
    # Alphabet maps to two tickers by name; the CUSIP override disambiguates.
    idx = {"alphabet": ["GOOGL", "GOOG"]}
    assert universe._resolve_ticker("Alphabet Inc.", "02079K305", idx) == "GOOGL"
    assert universe._resolve_ticker("Alphabet Inc.", "02079K107", idx) == "GOOG"


def test_resolve_unique_name_match():
    idx = {"apple": ["AAPL"]}
    assert universe._resolve_ticker("Apple Inc.", "037833100", idx) == "AAPL"


def test_resolve_ambiguous_without_override_is_none():
    # Ambiguous name + no CUSIP override -> None (never guess into the universe).
    idx = {"comcast": ["CMCSA", "CCZ"]}
    assert universe._resolve_ticker("Comcast Corp.", "999999999", idx) is None


def test_resolve_unknown_name_is_none():
    assert universe._resolve_ticker("Nonexistent Co.", "000000000", {}) is None


def test_norm_name_strips_suffixes_and_punctuation():
    assert (universe._norm_name("Take-Two Interactive Software, Inc.")
            == "take two interactive software")
    assert universe._norm_name("QUALCOMM Inc./DE") == "qualcomm"
    assert universe._norm_name("Kraft Heinz Co. (The)") == "kraft heinz"


# ---- as-of / range / end -----------------------------------------------------


def test_as_of_picks_most_recent_period_on_or_before(monkeypatch):
    _patch(monkeypatch)
    assert universe.nasdaq100_as_of("2026-02-15") == ["AAA", "BBB", "CCC"]  # the 2025-12-31 filing
    assert universe.nasdaq100_as_of("2025-06-30") == ["AAA", "BBB", "DDD"]  # the 2024-12-31 filing


def test_as_of_before_earliest_falls_back_to_earliest(monkeypatch):
    _patch(monkeypatch)
    assert universe.nasdaq100_as_of("2010-01-01") == ["AAA", "BBB", "DDD"]


def test_current_is_latest_filing(monkeypatch):
    _patch(monkeypatch)
    assert universe.current_nasdaq100() == ["AAA", "BBB", "CCC"]


def test_members_in_range_unions_window(monkeypatch):
    _patch(monkeypatch)
    # window spanning the DDD->CCC turnover includes both
    assert universe.nasdaq100_members_in_range("2024-12-31", "2026-03-31") == [
        "AAA", "BBB", "CCC", "DDD",
    ]


def test_membership_end_for_departed_and_current(monkeypatch):
    _patch(monkeypatch)
    assert universe.membership_end("CCC") is None          # in the latest filing
    assert universe.membership_end("DDD") == "2024-12-31"   # dropped after 2024
    assert universe.membership_end("ZZZ") is None           # never present
