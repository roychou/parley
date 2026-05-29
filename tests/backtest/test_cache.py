import pytest

from src.backtest.cache import SignalCache, cached_signal
from src.schemas.fundamentals import FundamentalsAnalysis


def _signal(ticker: str, signal: str = "BULLISH", confidence: float = 0.7) -> FundamentalsAnalysis:
    return FundamentalsAnalysis(
        specialist="fundamentals",
        ticker=ticker,
        signal=signal,
        confidence=confidence,
        reasoning="Synthetic reasoning padded to satisfy the min_length=50 constraint.",
        as_of="2026-01-09",
        rev_growth_yoy=0.12,
        pe_ratio=18.0,
        profit_margin=0.22,
        debt_to_equity=0.4,
    )


# ==========================================
# SIGNAL CACHE
# ==========================================


def test_cache_miss_returns_none(tmp_path):
    cache = SignalCache(tmp_path)
    assert cache.get("fundamentals", "AAPL", "2025-07-30_pe-fair", FundamentalsAnalysis) is None


def test_cache_set_and_get_round_trip(tmp_path):
    cache = SignalCache(tmp_path)
    original = _signal("AAPL", "BULLISH", 0.85)

    cache.set("fundamentals", "AAPL", "2025-07-30_pe-fair", original)
    retrieved = cache.get("fundamentals", "AAPL", "2025-07-30_pe-fair", FundamentalsAnalysis)

    assert retrieved is not None
    assert retrieved.ticker == "AAPL"
    assert retrieved.signal == "BULLISH"
    assert retrieved.confidence == pytest.approx(0.85)
    assert retrieved.reasoning == original.reasoning


def test_cache_isolates_by_data_version(tmp_path):
    """Different data_version (e.g. P/E crossed a band) is a different key."""
    cache = SignalCache(tmp_path)
    cache.set("fundamentals", "AAPL", "2025-07-30_pe-fair", _signal("AAPL"))

    assert cache.get("fundamentals", "AAPL", "2025-07-30_pe-high", FundamentalsAnalysis) is None
    assert cache.get("fundamentals", "AAPL", "2025-07-30_pe-fair", FundamentalsAnalysis) is not None


def test_cache_isolates_by_kind(tmp_path):
    """The same ticker/version under a different kind is a separate entry."""
    cache = SignalCache(tmp_path)
    cache.set("fundamentals", "AAPL", "k", _signal("AAPL"))
    assert cache.get("technicals", "AAPL", "k", FundamentalsAnalysis) is None


def test_per_kind_version_isolation(tmp_path):
    """Bumping one specialist's version leaves the other specialist's cache warm."""
    cache_a = SignalCache(tmp_path, versions={"fundamentals": "v1", "technicals": "v1"})
    cache_a.set("fundamentals", "AAPL", "k", _signal("AAPL"))
    cache_a.set("technicals", "AAPL", "k", _signal("AAPL"))

    # Bump only technicals to v2; fundamentals stays v1.
    cache_b = SignalCache(tmp_path, versions={"fundamentals": "v1", "technicals": "v2"})
    assert cache_b.get("fundamentals", "AAPL", "k", FundamentalsAnalysis) is not None  # warm
    assert cache_b.get("technicals", "AAPL", "k", FundamentalsAnalysis) is None  # invalidated


def test_cache_writes_to_kind_version_path(tmp_path):
    cache = SignalCache(tmp_path, default_version="v3")
    cache.set("fundamentals", "MSFT", "2026-02-13_pe-low", _signal("MSFT"))

    expected = tmp_path / "fundamentals" / "v3" / "MSFT_2026-02-13_pe-low.json"
    assert expected.exists()


def test_cache_corrupt_file_returns_none(tmp_path):
    cache = SignalCache(tmp_path)
    path = tmp_path / "fundamentals" / "v1" / "AAPL_k.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{")

    assert cache.get("fundamentals", "AAPL", "k", FundamentalsAnalysis) is None


# ==========================================
# cached_signal
# ==========================================


@pytest.mark.asyncio
async def test_cached_signal_computes_on_miss(tmp_path):
    cache = SignalCache(tmp_path)
    calls = []

    async def compute():
        calls.append(1)
        return _signal("AAPL")

    result = await cached_signal(cache, "fundamentals", "AAPL", "k", FundamentalsAnalysis, compute)

    assert result.ticker == "AAPL"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cached_signal_hits_cache_on_second_call(tmp_path):
    cache = SignalCache(tmp_path)
    calls = []

    async def compute():
        calls.append(1)
        return _signal("AAPL")

    await cached_signal(cache, "fundamentals", "AAPL", "k", FundamentalsAnalysis, compute)
    await cached_signal(cache, "fundamentals", "AAPL", "k", FundamentalsAnalysis, compute)

    # Computed exactly once; the second call hit the cache.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cached_signal_none_cache_always_computes(tmp_path):
    calls = []

    async def compute():
        calls.append(1)
        return _signal("AAPL")

    await cached_signal(None, "fundamentals", "AAPL", "k", FundamentalsAnalysis, compute)
    await cached_signal(None, "fundamentals", "AAPL", "k", FundamentalsAnalysis, compute)

    assert len(calls) == 2
