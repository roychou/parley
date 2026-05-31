"""
Portfolio state machine for the backtest.

Portfolio holds state (cash, open positions, closed-trade history, equity curve)
and exposes primitive operations (open, close, mark_to_market, close_all).

Position sizing is NOT Portfolio's responsibility. Strategies decide how many
dollars to open each position with; Portfolio just executes. This split keeps
Portfolio dumb and lets strategies (multi-agent confidence-weighted, baselines
equal-weight) share the same Portfolio without modification.

Stop-loss is applied during mark_to_market when stop_loss_pct is set. Pass None
to disable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.backtest.costs import CostModel

logger = logging.getLogger(__name__)


# ==========================================
# DATA MODELS (frozen — immutable records)
# ==========================================


@dataclass(frozen=True)
class Position:
    """An open position. Created on open, removed on close.

    `entry_price` is the actual fill price (already slippage-adjusted). `dollars_at_entry`
    is the notional put to work at that fill; `cost_at_entry` is the entry commission
    (cash), tracked so the closed-trade P&L can be reported net of both legs' costs.
    """
    ticker: str
    entry_date: str
    entry_price: float
    dollars_at_entry: float
    cost_at_entry: float = 0.0


@dataclass(frozen=True)
class Trade:
    """A closed-trade record. Appended to closed_trades on close."""
    ticker: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str          # "SELL_signal" | "stop_loss" | "end_of_backtest"
    dollars_at_entry: float
    realized_pnl_pct: float   # (exit_price - entry_price) / entry_price


@dataclass(frozen=True)
class EquitySnapshot:
    """Portfolio value at a point in time. Appended on each mark_to_market call."""
    date: str
    cash: float
    positions_value: float
    total_value: float


# ==========================================
# THE STATE MACHINE
# ==========================================


class Portfolio:
    """Mutable state container for a single backtest run.

    Not thread-safe. Each backtest strategy gets its own Portfolio instance.
    """

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        max_positions: int = 10,
        cost_model: CostModel | None = None,
    ):
        self.initial_cash = initial_cash
        self.max_positions = max_positions
        # Default frictionless: a Portfolio without a cost model behaves exactly as
        # before (slippage 0, commission 0), so zero-cost callers are unchanged.
        self.costs = cost_model or CostModel()

        self.cash: float = initial_cash
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[Trade] = []
        self.equity_curve: list[EquitySnapshot] = []
        self.dividends_received: float = 0.0  # cumulative cash dividends (total-return)

    # ---------- queries ----------

    def can_open(self, ticker: str) -> bool:
        """True if a new position can be opened: room in max_positions and not already held."""
        return ticker not in self.positions and len(self.positions) < self.max_positions

    def total_value(self, prices: dict[str, float]) -> float:
        """Current total portfolio value at the given prices.

        For positions with no price in `prices`, falls back to entry_price (no mark).
        """
        positions_value = sum(
            self._position_value(pos, prices.get(ticker, pos.entry_price))
            for ticker, pos in self.positions.items()
        )
        return self.cash + positions_value

    # ---------- mutations ----------

    def open(self, ticker: str, date: str, price: float, dollars: float) -> bool:
        """Open a position. Returns True if opened, False if rejected.

        Rejection cases: ticker already held, max_positions reached, insufficient cash,
        non-positive dollars or price.
        """
        if dollars <= 0 or price <= 0:
            logger.warning(f"open rejected: invalid dollars={dollars} or price={price} for {ticker}")
            return False
        if not self.can_open(ticker):
            logger.debug(f"open rejected: cannot open {ticker} (held or at max)")
            return False

        # Adverse fill (buy pays up) + entry commission charged in cash. Total cash
        # outlay is the notional plus commission; reject if it exceeds available cash.
        fill = self.costs.fill_price(price, "BUY")
        commission = self.costs.commission(dollars, fill)
        if dollars + commission > self.cash:
            logger.debug(
                f"open rejected: insufficient cash for {ticker} "
                f"(need {dollars + commission}, have {self.cash})"
            )
            return False

        self.cash -= dollars + commission
        self.positions[ticker] = Position(
            ticker=ticker,
            entry_date=date,
            entry_price=fill,
            dollars_at_entry=dollars,
            cost_at_entry=commission,
        )
        return True

    def close(self, ticker: str, date: str, price: float, reason: str) -> bool:
        """Close an existing position. Returns True if closed, False if not held."""
        if ticker not in self.positions:
            return False

        pos = self.positions[ticker]
        # Adverse fill (sell receives less) + exit commission. P&L is reported NET of
        # both legs' costs: proceeds (exit value minus exit commission) vs. cost basis
        # (entry notional plus entry commission). With a frictionless model this
        # reduces to the gross (exit-entry)/entry, so zero-cost callers are unchanged.
        fill = self.costs.fill_price(price, "SELL")
        exit_dollars = self._position_value(pos, fill)
        commission = self.costs.commission(exit_dollars, fill)
        self.cash += exit_dollars - commission

        cost_basis = pos.dollars_at_entry + pos.cost_at_entry
        proceeds = exit_dollars - commission
        pnl_pct = (proceeds - cost_basis) / cost_basis

        self.closed_trades.append(Trade(
            ticker=ticker,
            entry_date=pos.entry_date,
            entry_price=pos.entry_price,
            exit_date=date,
            exit_price=fill,
            exit_reason=reason,
            dollars_at_entry=pos.dollars_at_entry,
            realized_pnl_pct=pnl_pct,
        ))
        del self.positions[ticker]
        return True

    def mark_to_market(
        self,
        prices: dict[str, float],
        date: str,
        stop_loss_pct: float | None = -0.20,
    ) -> list[str]:
        """Update equity curve and apply stop-loss if configured.

        Returns the list of tickers that were stopped out on this MTM call.
        Pass `stop_loss_pct=None` to disable stop-loss.
        """
        stopped_out: list[str] = []
        if stop_loss_pct is not None:
            # iterate over a snapshot since close() mutates the dict
            for ticker, pos in list(self.positions.items()):
                if ticker not in prices:
                    continue  # no price — skip stop-loss check this period
                current_price = prices[ticker]
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                if pnl_pct <= stop_loss_pct:
                    self.close(ticker, date, current_price, "stop_loss")
                    stopped_out.append(ticker)

        positions_value = sum(
            self._position_value(pos, prices.get(ticker, pos.entry_price))
            for ticker, pos in self.positions.items()
        )
        self.equity_curve.append(EquitySnapshot(
            date=date,
            cash=self.cash,
            positions_value=positions_value,
            total_value=self.cash + positions_value,
        ))
        return stopped_out

    def apply_dividends(self, dividends_per_share: dict[str, float]) -> float:
        """Credit cash dividends for held positions on their ex-date (total return).

        `dividends_per_share` maps ticker -> split-adjusted dividend per share for the
        current date. Shares are fixed at entry (dollars_at_entry / entry_price, both
        split-adjusted), so the credit is shares × dividend. Dividends arrive as cash
        (as in a real account); the strategy redeploys them on its next rebalance — no
        automatic DRIP. Returns the total credited this call.
        """
        total = 0.0
        for ticker, pos in self.positions.items():
            div = dividends_per_share.get(ticker)
            if not div:
                continue
            shares = pos.dollars_at_entry / pos.entry_price
            amount = shares * div
            self.cash += amount
            self.dividends_received += amount
            total += amount
        return total

    def close_all(self, prices: dict[str, float], date: str) -> None:
        """Close every open position at end-of-backtest. Positions with no price are dropped."""
        for ticker in list(self.positions.keys()):
            if ticker in prices:
                self.close(ticker, date, prices[ticker], "end_of_backtest")
            else:
                # No price available; close at entry price (zero realized P&L)
                pos = self.positions[ticker]
                logger.warning(f"close_all: no price for {ticker}, closing at entry_price")
                self.close(ticker, date, pos.entry_price, "end_of_backtest")

    # ---------- internals ----------

    @staticmethod
    def _position_value(pos: Position, current_price: float) -> float:
        """Mark-to-market dollar value of a position at current_price."""
        return pos.dollars_at_entry * (current_price / pos.entry_price)
