"""
Backtest replay loop.

`run_backtest(config, price_loader, fundamentals_loader) -> BacktestResult`
iterates decision dates × strategies, executing each strategy independently
against its own Portfolio.

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


@dataclass(frozen=True)
class BacktestConfig:
    universe: list[str]
    decision_dates: list[str]   # sorted, YYYY-MM-DD strings
    strategies: list[Strategy]
    initial_cash: float = 100_000.0
    stop_loss_pct: float | None = -0.20
    extra_tickers: list[str] = field(default_factory=lambda: ["SPY"])  # baseline tickers outside the universe


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

    # Pre-load all price history (caller's loader is responsible for caching)
    all_tickers = list(config.universe) + list(config.extra_tickers)
    price_history: dict[str, dict[str, dict]] = {
        ticker: price_loader(ticker) for ticker in all_tickers
    }

    # One Portfolio per strategy
    outcomes: dict[str, StrategyOutcome] = {
        s.name: StrategyOutcome(
            name=s.name,
            portfolio=Portfolio(
                initial_cash=config.initial_cash,
                max_positions=s.max_positions,
            ),
            decisions=[],
        )
        for s in config.strategies
    }

    for date in config.decision_dates:
        prices_at_date = _prices_at(price_history, date)

        # Per-ticker fundamentals available at this date
        fundamentals_at_date: dict[str, ValuationSnapshot] = {}
        for ticker in config.universe:
            snap = fundamentals_loader(ticker, date)
            if snap is not None:
                fundamentals_at_date[ticker] = snap

        for strategy in config.strategies:
            outcome = outcomes[strategy.name]
            portfolio = outcome.portfolio

            # 1. Mark-to-market (applies stop-loss + snapshots equity curve)
            portfolio.mark_to_market(prices_at_date, date, stop_loss_pct=config.stop_loss_pct)

            # 2. Strategy decides
            actions = await strategy.decide_all(
                universe=config.universe,
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

    # End-of-backtest: close all remaining positions at final-date prices
    final_date = config.decision_dates[-1]
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
