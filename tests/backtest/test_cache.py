import pytest

from src.backtest.cache import DecisionCache, make_cached_provider
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis


def _decision(ticker: str, direction: str = "BUY", confidence: float = 0.7) -> Decision:
    signal = FundamentalsAnalysis(
        specialist="fundamentals",
        ticker=ticker,
        signal="BULLISH",
        confidence=confidence,
        reasoning="Synthetic reasoning padded to satisfy the min_length=50 constraint on SpecialistSignal.",
        as_of="2026-01-09",
        rev_growth_yoy=0.12,
        pe_ratio=18.0,
        profit_margin=0.22,
        debt_to_equity=0.4,
    )
    return Decision(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        rationale="Synthetic rationale padded to satisfy the min_length=50 constraint on Decision.",
        contributing_signals=[signal],
        as_of="2026-01-09",
    )


# ==========================================
# DECISION CACHE
# ==========================================


def test_cache_miss_returns_none(tmp_path):
    cache = DecisionCache(tmp_path)
    assert cache.get("AAPL", "2026-01-09") is None


def test_cache_set_and_get_round_trip(tmp_path):
    cache = DecisionCache(tmp_path)
    original = _decision("AAPL", "BUY", 0.85)

    cache.set("AAPL", "2026-01-09", original)
    retrieved = cache.get("AAPL", "2026-01-09")

    assert retrieved is not None
    assert retrieved.ticker == "AAPL"
    assert retrieved.direction == "BUY"
    assert retrieved.confidence == pytest.approx(0.85)
    assert retrieved.rationale == original.rationale


def test_cache_isolates_by_version(tmp_path):
    cache_v1 = DecisionCache(tmp_path, version="v1")
    cache_v2 = DecisionCache(tmp_path, version="v2")

    cache_v1.set("AAPL", "2026-01-09", _decision("AAPL", "BUY"))

    # v2 cache is empty even though v1 wrote to the same root
    assert cache_v2.get("AAPL", "2026-01-09") is None
    # v1 entry persists
    assert cache_v1.get("AAPL", "2026-01-09") is not None


def test_cache_writes_to_correct_path(tmp_path):
    cache = DecisionCache(tmp_path, version="v3")
    cache.set("MSFT", "2026-02-13", _decision("MSFT"))

    expected = tmp_path / "v3" / "MSFT_2026-02-13.json"
    assert expected.exists()


def test_cache_corrupt_file_returns_none(tmp_path):
    cache = DecisionCache(tmp_path)
    path = tmp_path / "v1" / "AAPL_2026-01-09.json"
    path.write_text("not valid json {{{")

    assert cache.get("AAPL", "2026-01-09") is None


# ==========================================
# CACHED PROVIDER
# ==========================================


@pytest.mark.asyncio
async def test_cached_provider_calls_supervisor_on_miss(tmp_path):
    cache = DecisionCache(tmp_path)
    calls = []

    async def supervisor(ticker, date):
        calls.append((ticker, date))
        return _decision(ticker, "BUY", 0.6)

    provider = make_cached_provider(supervisor, cache)
    decision = await provider("AAPL", "2026-01-09")

    assert decision.ticker == "AAPL"
    assert calls == [("AAPL", "2026-01-09")]


@pytest.mark.asyncio
async def test_cached_provider_hits_cache_on_second_call(tmp_path):
    cache = DecisionCache(tmp_path)
    calls = []

    async def supervisor(ticker, date):
        calls.append((ticker, date))
        return _decision(ticker, "BUY", 0.6)

    provider = make_cached_provider(supervisor, cache)
    await provider("AAPL", "2026-01-09")
    await provider("AAPL", "2026-01-09")

    # Supervisor called exactly once; second call hit the cache
    assert calls == [("AAPL", "2026-01-09")]


@pytest.mark.asyncio
async def test_cached_provider_preserves_decision_identity(tmp_path):
    cache = DecisionCache(tmp_path)

    async def supervisor(ticker, date):
        return _decision(ticker, "SELL", 0.42)

    provider = make_cached_provider(supervisor, cache)
    first = await provider("AAPL", "2026-01-09")
    second = await provider("AAPL", "2026-01-09")

    assert first.direction == second.direction == "SELL"
    assert first.confidence == second.confidence == pytest.approx(0.42)
