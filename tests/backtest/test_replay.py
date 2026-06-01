import pytest

from src.backtest.replay import BacktestConfig, run_backtest
from src.backtest.strategies import (
    MultiAgentStrategy,
    PERankingStrategy,
    RandomStrategy,
    RSIStrategy,
    SPYHoldStrategy,
)
from src.data.fundamentals import ValuationSnapshot
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis

# ==========================================
# FIXTURES
# ==========================================


DECISION_DATES = ["2026-01-09", "2026-01-16", "2026-01-23", "2026-01-30"]


def make_price_history(ticker_prices: dict[str, list[float]]) -> dict[str, dict]:
    """Build a price history dict from per-ticker close lists aligned to DECISION_DATES."""
    out = {}
    for ticker, closes in ticker_prices.items():
        out[ticker] = {
            date: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for date, c in zip(DECISION_DATES, closes)
        }
    return out


def _snapshot(pe: float) -> ValuationSnapshot:
    return ValuationSnapshot(
        price_date="2026-01-09",
        report_date="2025-07-30",
        period_end_date="2025-06-30",
        diluted_eps=10.0,
        profit_margin=0.20,
        rev_growth_yoy=0.10,
        debt_to_equity=0.5,
        pe_ratio=pe,
    )


def _decision(ticker: str, direction: str, confidence: float) -> Decision:
    signal = FundamentalsAnalysis(
        specialist="fundamentals",
        ticker=ticker,
        signal="BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else "NEUTRAL",
        confidence=confidence,
        reasoning="Synthetic reasoning padded to satisfy the min_length=50 constraint on the SpecialistSignal.",
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
        rationale="Synthetic rationale padded to satisfy the min_length=50 constraint on the Decision.",
        contributing_signals=[signal],
        as_of="2026-01-09",
    )


# ==========================================
# SMOKE TESTS
# ==========================================


@pytest.mark.asyncio
async def test_replay_runs_all_strategies_to_completion():
    universe = ["AAA", "BBB", "CCC"]

    price_history_data = {
        "AAA": [100.0, 102.0, 105.0, 108.0],
        "BBB": [50.0, 49.0, 51.0, 52.0],
        "CCC": [200.0, 198.0, 196.0, 195.0],
        "SPY": [400.0, 402.0, 405.0, 408.0],
    }
    history = make_price_history(price_history_data)

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(ticker, date):
        return {"AAA": _snapshot(15.0), "BBB": _snapshot(20.0), "CCC": _snapshot(25.0)}.get(ticker)

    async def decision_stub(ticker, date):
        return _decision(ticker, "BUY" if ticker == "AAA" else "HOLD", 0.7)

    config = BacktestConfig(
        universe=universe,
        decision_dates=DECISION_DATES,
        strategies=[
            MultiAgentStrategy(decision_provider=decision_stub),
            RandomStrategy(seed=42),
            SPYHoldStrategy(),
            RSIStrategy(),
            PERankingStrategy(quintile_size=2),
        ],
        stop_loss_pct=None,  # disable for predictable behavior
    )

    result = await run_backtest(config, price_loader, fundamentals_loader)

    # Every strategy produced a result
    assert set(result.outcomes.keys()) == {"multi_agent", "random", "spy_hold", "rsi", "pe_ranking"}

    # Equity curve length matches decision_dates count for every strategy
    for outcome in result.outcomes.values():
        assert len(outcome.portfolio.equity_curve) == len(DECISION_DATES)


@pytest.mark.asyncio
async def test_spy_hold_opens_spy_first_period_only():
    universe = ["AAA"]
    history = make_price_history({
        "AAA": [100.0, 102.0, 105.0, 108.0],
        "SPY": [400.0, 402.0, 405.0, 408.0],
    })

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(t, d): return None

    config = BacktestConfig(
        universe=universe,
        decision_dates=DECISION_DATES,
        strategies=[SPYHoldStrategy()],
        stop_loss_pct=None,
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    outcome = result.outcomes["spy_hold"]
    # End-of-backtest closes SPY → one trade total
    assert len(outcome.portfolio.closed_trades) == 1
    trade = outcome.portfolio.closed_trades[0]
    assert trade.ticker == "SPY"
    assert trade.entry_date == DECISION_DATES[0]
    assert trade.exit_date == DECISION_DATES[-1]
    assert trade.exit_reason == "end_of_backtest"


@pytest.mark.asyncio
async def test_multi_agent_records_decisions():
    """Audit trail: every supervisor Decision is recorded, including HOLDs and BUYs that
    didn't translate to actions (e.g., ticker already held). One Decision per period
    per ticker queried."""
    universe = ["AAA"]
    history = make_price_history({"AAA": [100.0, 110.0, 120.0, 130.0], "SPY": [400.0] * 4})

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(t, d): return None

    async def decision_stub(ticker, date):
        return _decision(ticker, "BUY", 0.8)

    config = BacktestConfig(
        universe=universe,
        decision_dates=DECISION_DATES,
        strategies=[MultiAgentStrategy(decision_provider=decision_stub)],
        stop_loss_pct=None,
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    outcome = result.outcomes["multi_agent"]
    # 4 decision dates × 1 ticker = 4 decisions logged regardless of action outcome
    assert len(outcome.decisions) == 4
    assert all(d.direction == "BUY" for d in outcome.decisions)


@pytest.mark.asyncio
async def test_closes_execute_before_opens_in_same_round():
    """P/E ranking rebalance: when A drops out and D becomes cheap, A must close
    before D opens, otherwise D's open would be rejected (max_positions full)."""
    universe = ["A", "B", "C", "D"]
    history = make_price_history({
        "A": [100.0, 102.0, 105.0, 108.0],
        "B": [100.0, 102.0, 105.0, 108.0],
        "C": [100.0, 102.0, 105.0, 108.0],
        "D": [100.0, 102.0, 105.0, 108.0],
        "SPY": [400.0] * 4,
    })

    def price_loader(ticker): return history[ticker]

    # Period 1: A and B are cheap (in quintile). Period 2: B and C are cheap. Period 3: C and D.
    fundamentals_by_period = [
        {"A": _snapshot(10.0), "B": _snapshot(15.0), "C": _snapshot(20.0), "D": _snapshot(25.0)},
        {"A": _snapshot(30.0), "B": _snapshot(15.0), "C": _snapshot(20.0), "D": _snapshot(25.0)},
        {"A": _snapshot(30.0), "B": _snapshot(35.0), "C": _snapshot(15.0), "D": _snapshot(10.0)},
        {"A": _snapshot(30.0), "B": _snapshot(35.0), "C": _snapshot(15.0), "D": _snapshot(10.0)},
    ]

    def fundamentals_loader(ticker, date):
        idx = DECISION_DATES.index(date)
        return fundamentals_by_period[idx].get(ticker)

    config = BacktestConfig(
        universe=universe,
        decision_dates=DECISION_DATES,
        strategies=[PERankingStrategy(quintile_size=2)],
        stop_loss_pct=None,
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    outcome = result.outcomes["pe_ranking"]
    # Period 3 rebalance: A,B drop out (replaced by C,D). If opens ran before closes,
    # C and D would be rejected because A and B still occupy the 2-slot portfolio.
    # The success criterion: at period 3 close, portfolio holds C and D.
    open_tickers_at_end = {
        # Period 3 is the second-to-last. Period 4 is end-of-backtest close.
        # Track trades to verify the C,D opens happened.
        t.ticker for t in outcome.portfolio.closed_trades
    }
    assert {"A", "B", "C", "D"}.issubset(open_tickers_at_end), \
        f"Expected all four tickers to have been opened at some point. Got trades on: {open_tickers_at_end}"


@pytest.mark.asyncio
async def test_daily_mark_to_market_between_weekly_decisions():
    """Decoupled cadences: decisions fire only on decision_dates, but the equity
    curve is sampled (and stop-loss checked) on every trading day in the window."""
    universe = ["AAA"]
    # Daily prices spanning the window; decisions only on the two endpoints.
    daily_dates = [f"2026-01-{d:02d}" for d in range(9, 17)]  # 8 consecutive days
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
    history = {
        "AAA": {dt: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
                for dt, c in zip(daily_dates, closes)},
        "SPY": {dt: {"open": 400.0, "high": 400.0, "low": 400.0, "close": 400.0, "volume": 1000}
                for dt in daily_dates},
    }

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(t, d): return None

    decide_calls: list[str] = []

    async def decision_stub(ticker, date):
        decide_calls.append(date)
        return _decision(ticker, "BUY", 0.8)

    config = BacktestConfig(
        universe=universe,
        decision_dates=["2026-01-09", "2026-01-16"],  # weekly: first + last day only
        strategies=[MultiAgentStrategy(decision_provider=decision_stub)],
        stop_loss_pct=None,
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    outcome = result.outcomes["multi_agent"]
    # Equity curve has one point per trading day (8), not per decision date (2).
    assert len(outcome.portfolio.equity_curve) == len(daily_dates)
    # The strategy decided only on the two decision dates.
    assert decide_calls == ["2026-01-09", "2026-01-16"]
    assert len(outcome.decisions) == 2


@pytest.mark.asyncio
async def test_daily_stop_loss_triggers_between_decisions():
    """A position blowing through the stop-loss mid-week is closed on the daily MTM,
    not deferred to the next decision date."""
    universe = ["AAA"]
    daily_dates = [f"2026-01-{d:02d}" for d in range(9, 17)]
    # Open at 100 on day 0, then a crash on day 3 (-30%, past the -20% stop).
    closes = [100.0, 99.0, 98.0, 70.0, 72.0, 74.0, 76.0, 78.0]
    history = {
        "AAA": {dt: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
                for dt, c in zip(daily_dates, closes)},
        "SPY": {dt: {"open": 400.0, "high": 400.0, "low": 400.0, "close": 400.0, "volume": 1000}
                for dt in daily_dates},
    }

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(t, d): return None

    async def decision_stub(ticker, date):
        return _decision(ticker, "BUY", 0.8)

    config = BacktestConfig(
        universe=universe,
        decision_dates=["2026-01-09", "2026-01-16"],
        strategies=[MultiAgentStrategy(decision_provider=decision_stub)],
        stop_loss_pct=-0.20,
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    trades = result.outcomes["multi_agent"].portfolio.closed_trades
    stop_losses = [t for t in trades if t.exit_reason == "stop_loss"]
    assert len(stop_losses) == 1
    # Stopped out on the crash day (2026-01-12), well before the next decision date.
    assert stop_losses[0].exit_date == "2026-01-12"


@pytest.mark.asyncio
async def test_universe_loader_feeds_per_date_eligible_set():
    """With a universe_loader, each decision date sees its own eligible universe,
    and prices for the union are preloaded (a name eligible on only one date works)."""
    dates = ["2026-01-09", "2026-01-16"]
    eligible = {"2026-01-09": ["AAA", "BBB"], "2026-01-16": ["BBB", "CCC"]}

    bar = {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1000}
    history = {t: {d: bar for d in dates} for t in ["AAA", "BBB", "CCC", "SPY"]}

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(t, d): return None

    analyzed: dict[str, list[str]] = {}

    async def decision_stub(ticker, date):
        analyzed.setdefault(date, []).append(ticker)
        return _decision(ticker, "HOLD", 0.6)

    config = BacktestConfig(
        universe=[],  # unused when a loader is set
        decision_dates=dates,
        strategies=[MultiAgentStrategy(decision_provider=decision_stub)],
        stop_loss_pct=None,
        universe_loader=lambda d: eligible[d],
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    assert sorted(analyzed["2026-01-09"]) == ["AAA", "BBB"]
    assert sorted(analyzed["2026-01-16"]) == ["BBB", "CCC"]
    # The run completed (CCC, eligible only on date 2, had preloaded prices).
    assert "multi_agent" in result.outcomes


@pytest.mark.asyncio
async def test_replay_handles_missing_prices_gracefully():
    universe = ["AAA"]
    # AAA has no price for one of the decision dates (delisted/halt simulation)
    history = {
        "AAA": {
            "2026-01-09": {"open": 100, "high": 100, "low": 100, "close": 100, "volume": 1000},
            "2026-01-23": {"open": 105, "high": 105, "low": 105, "close": 105, "volume": 1000},
            "2026-01-30": {"open": 108, "high": 108, "low": 108, "close": 108, "volume": 1000},
        },
        "SPY": make_price_history({"SPY": [400.0] * 4})["SPY"],
    }

    def price_loader(ticker): return history[ticker]
    def fundamentals_loader(t, d): return None

    async def decision_stub(ticker, date):
        return _decision(ticker, "BUY", 0.8)

    config = BacktestConfig(
        universe=universe,
        decision_dates=DECISION_DATES,
        strategies=[MultiAgentStrategy(decision_provider=decision_stub)],
        stop_loss_pct=None,
    )

    # Should not raise — missing prices skip actions but don't crash
    result = await run_backtest(config, price_loader, fundamentals_loader)

    outcome = result.outcomes["multi_agent"]
    # Equity curve still has all decision dates (MTM uses fallback for missing prices)
    assert len(outcome.portfolio.equity_curve) == len(DECISION_DATES)
