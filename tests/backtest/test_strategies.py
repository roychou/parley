import math

import pytest

from src.backtest.portfolio import Portfolio
from src.backtest.strategies import (
    Action,
    MultiAgentStrategy,
    PERankingStrategy,
    RandomStrategy,
    RSIStrategy,
    SPYHoldStrategy,
    compute_rsi,
)
from src.data.fundamentals import ValuationSnapshot
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis


# ==========================================
# HELPERS
# ==========================================


def make_decision(ticker: str, direction: str, confidence: float = 0.5) -> Decision:
    """Build a minimal valid Decision for testing."""
    signal = FundamentalsAnalysis(
        specialist="fundamentals",
        ticker=ticker,
        signal="BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else "NEUTRAL",
        confidence=confidence,
        reasoning=(
            "Synthetic test reasoning long enough to satisfy the min_length=50 Pydantic constraint "
            "on the SpecialistSignal base schema."
        ),
        as_of="2026-01-09",
        rev_growth_yoy=0.10,
        pe_ratio=18.0,
        profit_margin=0.22,
        debt_to_equity=0.4,
    )
    return Decision(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        rationale=(
            "Synthetic test rationale long enough to satisfy the min_length=50 Pydantic constraint "
            "on the Decision schema."
        ),
        contributing_signals=[signal],
        as_of="2026-01-09",
    )


def make_decision_provider(decisions_by_ticker: dict[str, Decision]):
    """Return an async decision_provider stub for MultiAgentStrategy tests."""
    async def provider(ticker: str, date: str) -> Decision:
        return decisions_by_ticker[ticker]
    return provider


def make_snapshot(pe_ratio: float) -> ValuationSnapshot:
    return ValuationSnapshot(
        price_date="2026-01-09",
        report_date="2025-07-30",
        period_end_date="2025-06-30",
        diluted_eps=10.0,
        profit_margin=0.20,
        rev_growth_yoy=0.10,
        debt_to_equity=0.5,
        pe_ratio=pe_ratio,
    )


# ==========================================
# COMPUTE_RSI
# ==========================================


def test_compute_rsi_insufficient_data():
    assert compute_rsi([100.0, 101.0], window=14) is None


def test_compute_rsi_all_gains_returns_100():
    # Monotonic uptrend: avg_loss = 0 → RSI = 100
    closes = [100.0 + i for i in range(20)]
    assert compute_rsi(closes, window=14) == 100.0


def test_compute_rsi_in_valid_range():
    # Mixed up/down data should produce RSI in (0, 100)
    closes = [100.0, 102.0, 101.0, 103.0, 102.5, 104.0, 103.0, 105.0,
              104.0, 106.0, 105.0, 107.0, 106.0, 108.0, 107.0, 109.0]
    rsi = compute_rsi(closes, window=14)
    assert rsi is not None
    assert 0 < rsi < 100


# ==========================================
# MULTIAGENT — translation logic
# ==========================================


@pytest.mark.asyncio
async def test_multiagent_buy_high_confidence_opens_with_scaled_size():
    p = Portfolio(initial_cash=100_000, max_positions=10)
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "BUY", confidence=0.8)})
    strat = MultiAgentStrategy(decision_provider=provider, base_pct=0.10, floor=0.02, cap=0.15)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert len(actions) == 1
    a = actions[0]
    assert a.kind == "OPEN"
    assert a.ticker == "AAPL"
    # 10% × 0.8 = 8%, within floor and cap
    assert a.position_size_pct == pytest.approx(0.08)
    assert a.decision is not None


@pytest.mark.asyncio
async def test_multiagent_buy_below_floor_no_action():
    p = Portfolio(initial_cash=100_000)
    # 10% × 0.15 = 1.5%, below 2% floor
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "BUY", confidence=0.15)})
    strat = MultiAgentStrategy(decision_provider=provider, base_pct=0.10, floor=0.02, cap=0.15)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert actions == []


@pytest.mark.asyncio
async def test_multiagent_buy_above_cap_clamps():
    p = Portfolio(initial_cash=100_000)
    # 10% × 1.0 = 10%, with cap=0.05 → clamped to 5%
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "BUY", confidence=1.0)})
    strat = MultiAgentStrategy(decision_provider=provider, base_pct=0.10, floor=0.01, cap=0.05)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert len(actions) == 1
    assert actions[0].position_size_pct == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_multiagent_buy_already_held_no_action():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-02", price=150.0, dollars=10_000)
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "BUY", confidence=0.8)})
    strat = MultiAgentStrategy(decision_provider=provider)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert actions == []


@pytest.mark.asyncio
async def test_multiagent_sell_held_closes():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-02", price=150.0, dollars=10_000)
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "SELL", confidence=0.7)})
    strat = MultiAgentStrategy(decision_provider=provider)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert len(actions) == 1
    assert actions[0].kind == "CLOSE"
    assert actions[0].ticker == "AAPL"
    assert actions[0].reason == "SELL_signal"


@pytest.mark.asyncio
async def test_multiagent_sell_not_held_no_action():
    p = Portfolio(initial_cash=100_000)
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "SELL", confidence=0.7)})
    strat = MultiAgentStrategy(decision_provider=provider)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert actions == []


@pytest.mark.asyncio
async def test_multiagent_hold_no_action():
    p = Portfolio(initial_cash=100_000)
    provider = make_decision_provider({"AAPL": make_decision("AAPL", "HOLD", confidence=0.5)})
    strat = MultiAgentStrategy(decision_provider=provider)

    actions = await strat.decide_all(["AAPL"], "2026-01-09", {}, {}, p)

    assert actions == []


@pytest.mark.asyncio
async def test_multiagent_skips_candidate_that_errors():
    """One candidate failing to analyze (e.g. no point-in-time fundamentals) must not
    abort the period — it's skipped, the others still decide."""
    p = Portfolio(initial_cash=100_000)

    async def provider(ticker, date):
        if ticker == "BAD":
            raise ValueError(f"No fundamentals data available for {ticker} as of {date}")
        return make_decision(ticker, "BUY", confidence=0.8)

    strat = MultiAgentStrategy(decision_provider=provider)
    actions = await strat.decide_all(["GOOD", "BAD", "ALSOGOOD"], "2026-01-09", {}, {}, p)

    opened = {a.ticker for a in actions if a.kind == "OPEN"}
    assert opened == {"GOOD", "ALSOGOOD"}                       # BAD skipped, not fatal
    assert {d.ticker for d in strat.last_decisions} == {"GOOD", "ALSOGOOD"}


@pytest.mark.asyncio
async def test_multiagent_at_max_positions_skips_new_buys():
    p = Portfolio(initial_cash=1_000_000, max_positions=2)
    p.open("AAA", "2026-01-02", price=100.0, dollars=10_000)
    p.open("BBB", "2026-01-02", price=100.0, dollars=10_000)
    provider = make_decision_provider({"CCC": make_decision("CCC", "BUY", confidence=0.9)})
    strat = MultiAgentStrategy(decision_provider=provider)

    actions = await strat.decide_all(["CCC"], "2026-01-09", {}, {}, p)

    # CCC has a BUY but portfolio is full
    assert actions == []


# ==========================================
# RANDOM
# ==========================================


@pytest.mark.asyncio
async def test_random_deterministic_with_seed():
    p1 = Portfolio(initial_cash=100_000)
    p2 = Portfolio(initial_cash=100_000)
    strat1 = RandomStrategy(seed=42)
    strat2 = RandomStrategy(seed=42)

    universe = ["AAA", "BBB", "CCC", "DDD"]
    actions1 = await strat1.decide_all(universe, "2026-01-09", {}, {}, p1)
    actions2 = await strat2.decide_all(universe, "2026-01-09", {}, {}, p2)

    assert [(a.kind, a.ticker) for a in actions1] == [(a.kind, a.ticker) for a in actions2]


@pytest.mark.asyncio
async def test_random_only_closes_held_tickers():
    p = Portfolio(initial_cash=100_000)
    p.open("AAA", "2026-01-02", price=100.0, dollars=10_000)
    strat = RandomStrategy(seed=42)

    actions = await strat.decide_all(["AAA", "BBB"], "2026-01-09", {}, {}, p)

    for a in actions:
        if a.kind == "CLOSE":
            assert a.ticker in p.positions  # only closes things actually held


# ==========================================
# SPY HOLD
# ==========================================


@pytest.mark.asyncio
async def test_spyhold_opens_spy_on_first_call():
    p = Portfolio(initial_cash=100_000, max_positions=1)
    strat = SPYHoldStrategy()

    actions = await strat.decide_all([], "2026-01-09", {}, {}, p)

    assert len(actions) == 1
    assert actions[0].kind == "OPEN"
    assert actions[0].ticker == "SPY"
    assert actions[0].position_size_pct == 1.0


@pytest.mark.asyncio
async def test_spyhold_no_action_when_spy_already_held():
    p = Portfolio(initial_cash=100_000, max_positions=1)
    p.open("SPY", "2026-01-02", price=500.0, dollars=100_000)
    strat = SPYHoldStrategy()

    actions = await strat.decide_all([], "2026-01-09", {}, {}, p)

    assert actions == []


# ==========================================
# RSI
# ==========================================


def _price_history(dates_and_closes: list[tuple[str, float]]) -> dict[str, dict]:
    """Build a price history dict for one ticker from a list of (date, close)."""
    return {date: {"open": c, "high": c, "low": c, "close": c, "volume": 1000} for date, c in dates_and_closes}


@pytest.mark.asyncio
async def test_rsi_opens_oversold_ticker():
    p = Portfolio(initial_cash=100_000)
    # Construct a decline that produces RSI < 30
    closes = [(f"2026-01-{i:02d}", 100.0 - i * 0.5) for i in range(1, 20)]
    history = {"AAPL": _price_history(closes)}
    strat = RSIStrategy(oversold=30.0, overbought=70.0)

    actions = await strat.decide_all(["AAPL"], "2026-01-19", history, {}, p)

    assert len(actions) == 1
    assert actions[0].kind == "OPEN"
    assert actions[0].ticker == "AAPL"


@pytest.mark.asyncio
async def test_rsi_closes_overbought_held_position():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2025-12-01", price=100.0, dollars=10_000)
    # Construct an uptrend that produces RSI > 70
    closes = [(f"2026-01-{i:02d}", 100.0 + i * 0.5) for i in range(1, 20)]
    history = {"AAPL": _price_history(closes)}
    strat = RSIStrategy(oversold=30.0, overbought=70.0)

    actions = await strat.decide_all(["AAPL"], "2026-01-19", history, {}, p)

    assert len(actions) == 1
    assert actions[0].kind == "CLOSE"
    assert actions[0].ticker == "AAPL"
    assert actions[0].reason == "RSI_overbought"


@pytest.mark.asyncio
async def test_rsi_insufficient_history_no_action():
    p = Portfolio(initial_cash=100_000)
    history = {"AAPL": _price_history([("2026-01-01", 100.0), ("2026-01-02", 101.0)])}
    strat = RSIStrategy()

    actions = await strat.decide_all(["AAPL"], "2026-01-02", history, {}, p)

    assert actions == []


@pytest.mark.asyncio
async def test_rsi_respects_point_in_time_filter():
    p = Portfolio(initial_cash=100_000)
    # History includes dates AFTER the decision date; should be excluded
    closes = [(f"2026-01-{i:02d}", 100.0 - i * 0.5) for i in range(1, 25)]
    history = {"AAPL": _price_history(closes)}
    strat = RSIStrategy()

    # Decision date is well before the full history → should be insufficient data
    actions = await strat.decide_all(["AAPL"], "2026-01-05", history, {}, p)
    assert actions == []  # only 5 days available <= decision date, need 15+


# ==========================================
# P/E RANKING
# ==========================================


@pytest.mark.asyncio
async def test_pe_ranking_opens_lowest_quintile():
    p = Portfolio(initial_cash=100_000)
    # 5 tickers with P/Es 10, 15, 20, 25, 30 → quintile size 3 → opens 10, 15, 20
    fundamentals = {
        "A": make_snapshot(pe_ratio=10.0),
        "B": make_snapshot(pe_ratio=15.0),
        "C": make_snapshot(pe_ratio=20.0),
        "D": make_snapshot(pe_ratio=25.0),
        "E": make_snapshot(pe_ratio=30.0),
    }
    strat = PERankingStrategy(quintile_size=3)

    actions = await strat.decide_all(["A", "B", "C", "D", "E"], "2026-01-09", {}, fundamentals, p)

    opened = {a.ticker for a in actions if a.kind == "OPEN"}
    assert opened == {"A", "B", "C"}
    # Each opens at 1/3 = 33.3%
    for a in actions:
        if a.kind == "OPEN":
            assert a.position_size_pct == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_pe_ranking_closes_dropouts():
    p = Portfolio(initial_cash=100_000, max_positions=3)
    # Initially holding A and B. Now D becomes cheap; A drops out.
    p.open("A", "2026-01-02", price=100.0, dollars=10_000)
    p.open("B", "2026-01-02", price=100.0, dollars=10_000)
    fundamentals = {
        "A": make_snapshot(pe_ratio=30.0),  # was cheap, now expensive
        "B": make_snapshot(pe_ratio=12.0),
        "C": make_snapshot(pe_ratio=15.0),
        "D": make_snapshot(pe_ratio=10.0),
        "E": make_snapshot(pe_ratio=40.0),
    }
    strat = PERankingStrategy(quintile_size=3)

    actions = await strat.decide_all(["A", "B", "C", "D", "E"], "2026-01-09", {}, fundamentals, p)

    closed_tickers = {a.ticker for a in actions if a.kind == "CLOSE"}
    opened_tickers = {a.ticker for a in actions if a.kind == "OPEN"}

    # A dropped out, B stays (in quintile), C and D become new opens
    assert "A" in closed_tickers
    assert "B" not in closed_tickers  # B is in the new quintile, no change
    assert {"C", "D"}.issubset(opened_tickers)


@pytest.mark.asyncio
async def test_pe_ranking_skips_missing_or_invalid_pe():
    p = Portfolio(initial_cash=100_000)
    fundamentals = {
        "A": make_snapshot(pe_ratio=float("nan")),  # NaN
        "B": make_snapshot(pe_ratio=-5.0),          # negative (loss-making)
        "C": make_snapshot(pe_ratio=15.0),
        "D": make_snapshot(pe_ratio=20.0),
    }
    strat = PERankingStrategy(quintile_size=3)

    actions = await strat.decide_all(["A", "B", "C", "D"], "2026-01-09", {}, fundamentals, p)

    opened = {a.ticker for a in actions if a.kind == "OPEN"}
    # Only C and D have valid P/Es — only those eligible for ranking
    assert opened == {"C", "D"}
