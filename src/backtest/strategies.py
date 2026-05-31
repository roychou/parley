"""
Backtest strategies. Five strategies implementing a uniform `decide_all` interface.

Per-ticker decide() would not work for SPYHold or P/E ranking — both are cross-ticker
strategies that need to see the whole universe at once. The unified
`decide_all(universe, date, prices, fundamentals, portfolio) -> list[Action]`
shape handles both cases without per-strategy interface variation.

Strategies emit Actions (OPEN with size_pct, CLOSE with reason). The replay loop
translates Actions to Portfolio calls. Sizing logic lives in each strategy (where
it differs); execution logic lives in Portfolio (where it's uniform).

The multi-agent strategy takes a `decision_provider` callable so tests can stub
the LLM call. Production wires this to the supervisor (with caching by ticker+date).
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from typing import Awaitable, Callable, Literal, Protocol

from src.backtest.portfolio import Portfolio
from src.backtest.screen import FilingDatesFn, select_candidates
from src.data.fundamentals import ValuationSnapshot
from src.schemas import Decision

logger = logging.getLogger(__name__)

DecisionProvider = Callable[[str, str], Awaitable[Decision]]

# Substrings marking a SYSTEMIC failure — one that will recur on every subsequent
# call (billing/auth), so the run must abort rather than mask it as per-name skips.
_FATAL_ERROR_MARKERS = (
    "credit balance",        # out of API credits
    "authentication",        # bad/expired key
    "permission",            # permission denied
    "x-api-key", "api key",  # missing/invalid key
)


def _is_fatal_error(exc: Exception) -> bool:
    """True for systemic errors (billing/auth) that doom the rest of the run."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _FATAL_ERROR_MARKERS)


# ==========================================
# ACTION
# ==========================================


@dataclass(frozen=True)
class Action:
    """A position-management action emitted by a strategy.

    kind = "OPEN" or "CLOSE". position_size_pct is the fraction of current portfolio
    total_value to allocate (only meaningful for OPEN). reason is the exit cause for
    CLOSE. decision is the underlying Decision when available (multi-agent only) —
    None for non-LLM strategies.
    """
    kind: Literal["OPEN", "CLOSE"]
    ticker: str
    position_size_pct: float = 0.0
    reason: str = ""
    decision: Decision | None = None


# ==========================================
# STRATEGY PROTOCOL
# ==========================================


class Strategy(Protocol):
    """Structural contract every backtest strategy conforms to."""
    name: str
    max_positions: int

    async def decide_all(
        self,
        universe: list[str],
        date: str,
        price_history: dict[str, dict[str, dict]],   # ticker -> {date -> ohlcv}
        fundamentals_by_ticker: dict[str, ValuationSnapshot],
        portfolio: Portfolio,
    ) -> list[Action]:
        ...


# ==========================================
# RSI HELPER (pure-Python Wilder's smoothing)
# ==========================================


def compute_rsi(closes: list[float], window: int = 14) -> float | None:
    """RSI via Wilder's smoothing. Returns latest value, or None if insufficient data.

    Matches the pandas implementation in src/data/technicals.py mathematically.
    Reimplemented here to keep strategies module free of pandas in the tight loop.
    """
    if len(closes) < window + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))

    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window

    for i in range(window, len(gains)):
        avg_gain = (avg_gain * (window - 1) + gains[i]) / window
        avg_loss = (avg_loss * (window - 1) + losses[i]) / window

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ==========================================
# MULTI-AGENT (the system under test)
# ==========================================


class MultiAgentStrategy:
    """Runs the supervisor pipeline per ticker, sizes by confidence.

    Sizing: scaled = base_pct * confidence, with floor (skip if below) and cap.
    Default base=10%, floor=2%, cap=15% per the backtest design doc.

    Maintains `last_decisions` — the full list of decisions produced on the most
    recent decide_all call, regardless of whether they emitted actions. Replay
    loop reads this for the audit trail (every supervisor decision is logged,
    not just the ones that opened/closed positions).
    """
    name = "multi_agent"
    max_positions = 10

    def __init__(
        self,
        decision_provider: DecisionProvider,
        base_pct: float = 0.10,
        floor: float = 0.02,
        cap: float = 0.15,
        filing_dates_fn: FilingDatesFn | None = None,
        screen_lookback_days: int = 100,
    ):
        self.decision_provider = decision_provider
        self.base_pct = base_pct
        self.floor = floor
        self.cap = cap
        # Event-driven screen: when filing_dates_fn is provided, only analyze names
        # that filed since the last decision (plus current holdings). None -> analyze
        # the whole universe given (backward compatible).
        self.filing_dates_fn = filing_dates_fn
        self.screen_lookback_days = screen_lookback_days
        self._last_decision_date: str | None = None
        self.last_decisions: list[Decision] = []

    def _screen(self, universe: list[str], date: str, portfolio: Portfolio) -> list[str]:
        if self._last_decision_date is not None:
            window_start = self._last_decision_date
        else:
            y, m, d = map(int, date.split("-"))
            window_start = (_date(y, m, d) - timedelta(days=self.screen_lookback_days)).isoformat()
        return select_candidates(
            eligible=universe,
            held=list(portfolio.positions),
            window_start=window_start,
            window_end=date,
            filing_dates_fn=self.filing_dates_fn,
        )

    async def decide_all(
        self,
        universe: list[str],
        date: str,
        price_history: dict[str, dict[str, dict]],
        fundamentals_by_ticker: dict[str, ValuationSnapshot],
        portfolio: Portfolio,
    ) -> list[Action]:
        import asyncio
        if self.filing_dates_fn is not None:
            universe = self._screen(universe, date, portfolio)
            self._last_decision_date = date
        # Resilient fan-out: a single candidate that can't be analyzed (no
        # point-in-time fundamentals/technicals, or a transient blip) must not abort
        # the whole period at index scale — skip it (logged), it just gets no decision.
        # BUT a *systemic* error (exhausted credits, bad auth) affects every remaining
        # call, so swallowing it as N per-name skips would silently emit a fake summary
        # over a crippled subset. Those abort the run loudly instead.
        results = await asyncio.gather(
            *[self.decision_provider(ticker, date) for ticker in universe],
            return_exceptions=True,
        )
        decisions: list[Decision] = []
        for ticker, result in zip(universe, results):
            if isinstance(result, Exception):
                if _is_fatal_error(result):
                    raise result
                logger.warning(f"skipping {ticker} @ {date}: {type(result).__name__}: {result}")
                continue
            decisions.append(result)
        self.last_decisions = decisions
        actions: list[Action] = []
        for decision in decisions:
            actions.extend(self._translate(decision, portfolio))
        return actions

    def _translate(self, decision: Decision, portfolio: Portfolio) -> list[Action]:
        if decision.direction == "BUY":
            if decision.ticker in portfolio.positions:
                return []
            if not portfolio.can_open(decision.ticker):
                return []
            scaled = self.base_pct * decision.confidence
            if scaled < self.floor:
                return []
            scaled = min(scaled, self.cap)
            return [Action(
                kind="OPEN",
                ticker=decision.ticker,
                position_size_pct=scaled,
                decision=decision,
            )]
        if decision.direction == "SELL":
            if decision.ticker in portfolio.positions:
                return [Action(
                    kind="CLOSE",
                    ticker=decision.ticker,
                    reason="SELL_signal",
                    decision=decision,
                )]
            return []
        # HOLD → no action
        return []


# ==========================================
# RANDOM (no-information baseline)
# ==========================================


class RandomStrategy:
    """For each ticker each period, randomly pick BUY/HOLD/SELL. Equal-weight sizing."""
    name = "random"
    max_positions = 10

    def __init__(self, seed: int | None = None, position_size_pct: float = 0.10):
        self.rng = random.Random(seed)
        self.position_size_pct = position_size_pct

    async def decide_all(
        self,
        universe: list[str],
        date: str,
        price_history: dict[str, dict[str, dict]],
        fundamentals_by_ticker: dict[str, ValuationSnapshot],
        portfolio: Portfolio,
    ) -> list[Action]:
        actions: list[Action] = []
        for ticker in universe:
            choice = self.rng.choice(["BUY", "HOLD", "SELL"])
            if choice == "BUY" and ticker not in portfolio.positions and portfolio.can_open(ticker):
                actions.append(Action(
                    kind="OPEN",
                    ticker=ticker,
                    position_size_pct=self.position_size_pct,
                ))
            elif choice == "SELL" and ticker in portfolio.positions:
                actions.append(Action(kind="CLOSE", ticker=ticker, reason="random_SELL"))
        return actions


# ==========================================
# SPY BUY-AND-HOLD (market benchmark)
# ==========================================


class SPYHoldStrategy:
    """Buys SPY at 100% allocation on the first decision date; holds forever after."""
    name = "spy_hold"
    max_positions = 1
    ticker = "SPY"

    async def decide_all(
        self,
        universe: list[str],
        date: str,
        price_history: dict[str, dict[str, dict]],
        fundamentals_by_ticker: dict[str, ValuationSnapshot],
        portfolio: Portfolio,
    ) -> list[Action]:
        if self.ticker in portfolio.positions:
            return []
        if not portfolio.can_open(self.ticker):
            return []
        return [Action(kind="OPEN", ticker=self.ticker, position_size_pct=1.0)]


# ==========================================
# RSI (technicals-only baseline)
# ==========================================


class RSIStrategy:
    """RSI-driven mean-reversion. Oversold = BUY, overbought = SELL. Equal-weight."""
    name = "rsi"
    max_positions = 10

    def __init__(
        self,
        oversold: float = 30.0,
        overbought: float = 70.0,
        window: int = 14,
        position_size_pct: float = 0.10,
    ):
        self.oversold = oversold
        self.overbought = overbought
        self.window = window
        self.position_size_pct = position_size_pct

    async def decide_all(
        self,
        universe: list[str],
        date: str,
        price_history: dict[str, dict[str, dict]],
        fundamentals_by_ticker: dict[str, ValuationSnapshot],
        portfolio: Portfolio,
    ) -> list[Action]:
        actions: list[Action] = []
        for ticker in universe:
            ticker_prices = price_history.get(ticker, {})
            available_dates = sorted(d for d in ticker_prices if d <= date)
            if len(available_dates) < self.window + 1:
                continue
            closes = [ticker_prices[d]["close"] for d in available_dates]
            rsi = compute_rsi(closes, window=self.window)
            if rsi is None:
                continue
            if rsi < self.oversold and ticker not in portfolio.positions and portfolio.can_open(ticker):
                actions.append(Action(
                    kind="OPEN",
                    ticker=ticker,
                    position_size_pct=self.position_size_pct,
                ))
            elif rsi > self.overbought and ticker in portfolio.positions:
                actions.append(Action(kind="CLOSE", ticker=ticker, reason="RSI_overbought"))
        return actions


# ==========================================
# P/E RANKING (fundamentals-only baseline)
# ==========================================


class PERankingStrategy:
    """Each period, hold the lowest-P/E quintile of the universe. Equal-weight within quintile."""
    name = "pe_ranking"

    def __init__(self, quintile_size: int = 3):
        self.quintile_size = quintile_size

    @property
    def max_positions(self) -> int:
        return self.quintile_size

    async def decide_all(
        self,
        universe: list[str],
        date: str,
        price_history: dict[str, dict[str, dict]],
        fundamentals_by_ticker: dict[str, ValuationSnapshot],
        portfolio: Portfolio,
    ) -> list[Action]:
        import math

        ranked: list[tuple[float, str]] = []
        for ticker in universe:
            fund = fundamentals_by_ticker.get(ticker)
            if fund is None or fund.pe_ratio is None or math.isnan(fund.pe_ratio) or fund.pe_ratio <= 0:
                continue
            ranked.append((fund.pe_ratio, ticker))
        ranked.sort()

        target_quintile = {ticker for _, ticker in ranked[: self.quintile_size]}

        actions: list[Action] = []
        # Close positions that fell out of the quintile
        for ticker in list(portfolio.positions.keys()):
            if ticker not in target_quintile:
                actions.append(Action(
                    kind="CLOSE",
                    ticker=ticker,
                    reason="dropped_from_quintile",
                ))
        # Open new positions for tickers in the quintile not yet held
        size_pct = 1.0 / self.quintile_size if self.quintile_size > 0 else 0.0
        for ticker in target_quintile:
            if ticker not in portfolio.positions and portfolio.can_open(ticker):
                actions.append(Action(
                    kind="OPEN",
                    ticker=ticker,
                    position_size_pct=size_pct,
                ))
        return actions
