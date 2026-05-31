"""
Backtest replay loop.

`run_backtest(config, price_loader, fundamentals_loader) -> BacktestResult`
iterates trading days × strategies, executing each strategy independently
against its own Portfolio.

Decoupled cadences: strategies (re)decide only on `config.decision_dates`
(expensive, LLM-driven), while mark-to-market — stop-loss + equity snapshot —
runs on every trading day in the window (cheap, and risk must be checked
tightly). This means the equity curve is sampled daily, so Sharpe annualizes
with `config.periods_per_year` (252), not weekly. When the price data contains
only the decision dates, the two cadences coincide and behaviour is unchanged.

Two sequencing invariants worth being explicit about:

1. Sizing snapshot. portfolio_value_for_sizing is captured once before any
   actions for the current date are applied. All OPEN actions in that round
   size against this snapshot. Without this, the first open shrinks cash
   and subsequent opens get smaller positions than the strategy intended.

2. Closes before opens. Within a round, every CLOSE executes before any
   OPEN. This frees cash and slots for openers that would otherwise be
   rejected (e.g., P/E ranking's rebalance: CLOSE A + OPEN C + OPEN D).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.backtest.costs import CostModel
from src.backtest.portfolio import Portfolio
from src.backtest.strategies import Action, Strategy
from src.data.fundamentals import ValuationSnapshot
from src.schemas import Decision

logger = logging.getLogger(__name__)


# ==========================================
# CONFIG + RESULTS
# ==========================================


PriceLoader = Callable[[str], dict[str, dict]]  # ticker -> {date -> ohlcv dict}
FundamentalsLoader = Callable[[str, str], ValuationSnapshot | None]  # (ticker, date) -> snapshot or None
UniverseLoader = Callable[[str], list[str]]  # date -> eligible tickers as of that date


@dataclass(frozen=True)
class BacktestConfig:
    universe: list[str]
    decision_dates: list[str]   # sorted, YYYY-MM-DD strings — when strategies (re)decide
    strategies: list[Strategy]
    initial_cash: float = 100_000.0
    stop_loss_pct: float | None = -0.20
    extra_tickers: list[str] = field(default_factory=lambda: ["SPY"])  # baseline tickers outside the universe
    periods_per_year: int = 252  # daily equity curve (per MTM); Sharpe annualized daily
    # Point-in-time universe. When set, the eligible universe is recomputed each
    # decision date (e.g. S&P 500 as-of). When None, the static `universe` is used
    # for every date (backward compatible).
    universe_loader: UniverseLoader | None = None
    # Transaction costs applied to every fill, for ALL strategies (the multi-agent
    # system and the baselines pay the same costs — a fair, after-friction fight).
    # None = frictionless (backward compatible).
    cost_model: CostModel | None = None

    def universe_at(self, date: str) -> list[str]:
        return self.universe_loader(date) if self.universe_loader is not None else self.universe


@dataclass
class StrategyOutcome:
    """Per-strategy result. Portfolio holds the equity curve and trade log."""
    name: str
    portfolio: Portfolio
    decisions: list[Decision]   # populated when strategy emits decisions (multi-agent only)


@dataclass
class BacktestResult:
    config: BacktestConfig
    outcomes: dict[str, StrategyOutcome]


# ==========================================
# THE REPLAY LOOP
# ==========================================


async def run_backtest(
    config: BacktestConfig,
    price_loader: PriceLoader,
    fundamentals_loader: FundamentalsLoader,
) -> BacktestResult:
    """Replay strategies over historical decision dates against a simulated portfolio.

    price_loader: returns the full OHLCV dict for a ticker. Strategies do their
    own point-in-time filtering against `date`.
    fundamentals_loader: returns the latest filing available at `date` (point-in-time
    on filingDate <= date), or None if not available.
    """
    if not config.decision_dates:
        raise ValueError("decision_dates is empty")

    # Pre-load all price history (caller's loader is responsible for caching).
    # Union the per-date eligible universes so a name held after it leaves the
    # universe still has prices for mark-to-market. With no universe_loader this
    # is just the static universe.
    universe_union: set[str] = set(config.extra_tickers)
    for d in config.decision_dates:
        universe_union.update(config.universe_at(d))
    price_history: dict[str, dict[str, dict]] = {
        ticker: price_loader(ticker) for ticker in sorted(universe_union)
    }

    # One Portfolio per strategy
    outcomes: dict[str, StrategyOutcome] = {
        s.name: StrategyOutcome(
            name=s.name,
            portfolio=Portfolio(
                initial_cash=config.initial_cash,
                max_positions=s.max_positions,
                cost_model=config.cost_model,
            ),
            decisions=[],
        )
        for s in config.strategies
    }

    # Decoupled cadences: strategies (re)decide only on decision_dates (expensive,
    # LLM-driven), but mark-to-market — stop-loss checks + equity snapshots — runs
    # every trading day in the window (cheap, and risk must be tight). The trading
    # calendar is the union of every ticker's price dates within the decision window.
    decision_set = set(config.decision_dates)
    window_start, window_end = config.decision_dates[0], config.decision_dates[-1]
    trading_days = sorted({
        d
        for history in price_history.values()
        for d in history
        if window_start <= d <= window_end
    })
    if not trading_days:
        raise ValueError("no price data within the decision window")

    for date in trading_days:
        prices_at_date = _prices_at(price_history, date)
        is_decision_day = date in decision_set

        # Eligible universe as of this decision date (point-in-time when a loader
        # is configured; the static universe otherwise).
        universe_today = config.universe_at(date) if is_decision_day else []

        # Fundamentals are only consumed when strategies decide.
        fundamentals_at_date: dict[str, ValuationSnapshot] = {}
        if is_decision_day:
            for ticker in universe_today:
                snap = fundamentals_loader(ticker, date)
                if snap is not None:
                    fundamentals_at_date[ticker] = snap

        for strategy in config.strategies:
            outcome = outcomes[strategy.name]
            portfolio = outcome.portfolio

            # 1. Mark-to-market every trading day (applies stop-loss + snapshots equity).
            portfolio.mark_to_market(prices_at_date, date, stop_loss_pct=config.stop_loss_pct)

            # 2. Decide only on decision days.
            if not is_decision_day:
                continue

            actions = await strategy.decide_all(
                universe=universe_today,
                date=date,
                price_history=price_history,
                fundamentals_by_ticker=fundamentals_at_date,
                portfolio=portfolio,
            )

            # 3. Log all decisions for the audit trail. Strategies that produce
            # explicit Decision objects expose them via `last_decisions`; HOLDs
            # that didn't translate to actions still get recorded here.
            last = getattr(strategy, "last_decisions", None)
            if last:
                outcome.decisions.extend(last)

            # 4. Snapshot value for sizing BEFORE applying any actions
            portfolio_value_for_sizing = portfolio.total_value(prices_at_date)

            # 5. Execute closes first, then opens
            closes = [a for a in actions if a.kind == "CLOSE"]
            opens = [a for a in actions if a.kind == "OPEN"]
            _execute_actions(closes, portfolio, prices_at_date, date, portfolio_value_for_sizing)
            _execute_actions(opens, portfolio, prices_at_date, date, portfolio_value_for_sizing)

    # End-of-backtest: close all remaining positions at the final trading day's prices
    final_date = trading_days[-1]
    final_prices = _prices_at(price_history, final_date)
    for outcome in outcomes.values():
        outcome.portfolio.close_all(final_prices, final_date)

    return BacktestResult(config=config, outcomes=outcomes)


# ==========================================
# INTERNALS
# ==========================================


def _prices_at(price_history: dict[str, dict[str, dict]], date: str) -> dict[str, float]:
    """Extract close prices for `date` across all tickers that have data on that date."""
    out: dict[str, float] = {}
    for ticker, history in price_history.items():
        row = history.get(date)
        if row is not None:
            out[ticker] = float(row["close"])
    return out


def _execute_actions(
    actions: list[Action],
    portfolio: Portfolio,
    prices: dict[str, float],
    date: str,
    portfolio_value_for_sizing: float,
) -> None:
    for action in actions:
        price = prices.get(action.ticker)
        if price is None:
            logger.debug(f"skipping action {action.kind} {action.ticker}@{date}: no price available")
            continue
        if action.kind == "OPEN":
            dollars = portfolio_value_for_sizing * action.position_size_pct
            portfolio.open(action.ticker, date, price, dollars)
        elif action.kind == "CLOSE":
            portfolio.close(action.ticker, date, price, action.reason or "unspecified")
