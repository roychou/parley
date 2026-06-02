"""
Forward paper-trading book — persistent state for the only clean edge evaluation.

The backtest can't establish this strategy's edge (training-data contamination — see
notes/productization.md 0.0): the clean post-cutoff window is ~4 months. The honest
path is **forward** paper trading: every decision is on data the models have never
seen, so it accrues an uncontaminated track record — but only with calendar time, so
the machinery has to exist and run on a cadence well before there's anything to judge.

Unlike the backtest (one in-memory run), forward paper must **persist across runs**: a
scheduled session loads the book, marks-to-market at current prices, optionally decides,
and saves. This module is that persistence + a single forward step. It reuses the
battle-tested `Portfolio` (open/close/MTM/costs/dividends) and the multi-agent
strategy's sizing, so paper behaviour matches the backtest exactly.

Deliberately out of scope here (next steps): the live **price source** (the open
vendor decision — yfinance is acceptable for *forward* trading since survivorship bias
is a backtest-only concern; or an Alpaca paper account for data + fills) and the
**scheduler**. Both plug in around this core.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.backtest.costs import CostModel
from src.backtest.portfolio import EquitySnapshot, Portfolio, Position, Trade
from src.backtest.replay import _execute_actions
from src.backtest.strategies import MultiAgentStrategy
from src.risk import RiskConfig
from src.schemas import Decision

DEFAULT_BOOK_PATH = Path("data/forward/paper_book.json")


@dataclass
class PaperBook:
    """Serializable snapshot of a paper-trading account, persisted between sessions."""
    initial_cash: float = 100_000.0
    max_positions: int = 10
    cash: float = 100_000.0
    positions: dict[str, dict] = field(default_factory=dict)        # ticker -> Position fields
    closed_trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    dividends_received: float = 0.0
    decision_log: list[dict] = field(default_factory=list)          # audit trail of every decision
    last_run_date: str | None = None

    # -- persistence --
    @classmethod
    def load(cls, path: Path = DEFAULT_BOOK_PATH) -> PaperBook:
        if not Path(path).exists():
            return cls()
        return cls(**json.loads(Path(path).read_text()))

    def save(self, path: Path = DEFAULT_BOOK_PATH) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    # -- Portfolio bridge (reuse the backtest state machine) --
    def to_portfolio(self, cost_model: CostModel | None = None) -> Portfolio:
        p = Portfolio(self.initial_cash, self.max_positions, cost_model)
        p.cash = self.cash
        p.positions = {t: Position(**d) for t, d in self.positions.items()}
        p.closed_trades = [Trade(**d) for d in self.closed_trades]
        p.equity_curve = [EquitySnapshot(**d) for d in self.equity_curve]
        p.dividends_received = self.dividends_received
        return p

    def update_from_portfolio(self, p: Portfolio) -> None:
        self.cash = p.cash
        self.positions = {t: asdict(pos) for t, pos in p.positions.items()}
        self.closed_trades = [asdict(t) for t in p.closed_trades]
        self.equity_curve = [asdict(s) for s in p.equity_curve]
        self.dividends_received = p.dividends_received

    def log_decisions(self, date: str, decisions: list[Decision]) -> None:
        for d in decisions:
            self.decision_log.append({
                "date": date, "ticker": d.ticker,
                "direction": d.direction, "confidence": d.confidence,
                "rationale": d.rationale,  # persist the "why" for the audit trail
            })


def run_forward_step(
    book: PaperBook,
    date: str,
    prices: dict[str, float],
    decisions: list[Decision] | None = None,
    *,
    cost_model: CostModel | None = None,
    dividends: dict[str, float] | None = None,
    stop_loss_pct: float | None = -0.20,
    base_pct: float = 0.10,
    floor: float = 0.02,
    cap: float = 0.15,
    risk_config: RiskConfig | None = None,
    vols: dict[str, float | None] | None = None,
) -> Portfolio:
    """Advance the paper book one session: credit dividends, mark-to-market, and — when
    `decisions` are supplied (a decision day) — translate and execute them.

    Mutates `book` in place (caller saves). Sizing matches the backtest exactly: with a
    risk_config, the risk layer (inverse-vol + caps + drawdown governor) sizes BUYs as a
    set (needs `vols` per BUY); without one, the flat base_pct×confidence sizing.
    Preserves the replay's closes-before-opens / single-sizing-snapshot invariants.
    """
    portfolio = book.to_portfolio(cost_model)

    if dividends:
        portfolio.apply_dividends(dividends)
    portfolio.mark_to_market(prices, date, stop_loss_pct=stop_loss_pct)

    if decisions:
        from src.data.sectors import sector_of
        translator = MultiAgentStrategy(
            decision_provider=None, base_pct=base_pct, floor=floor, cap=cap,
            risk_config=risk_config, sector_map_fn=sector_of,
        )
        actions = translator.build_actions(decisions, portfolio, vols=vols)
        sizing_value = portfolio.total_value(prices)  # snapshot before any action
        closes = [a for a in actions if a.kind == "CLOSE"]
        opens = [a for a in actions if a.kind == "OPEN"]
        _execute_actions(closes, portfolio, prices, date, sizing_value)
        _execute_actions(opens, portfolio, prices, date, sizing_value)
        book.log_decisions(date, decisions)

    book.update_from_portfolio(portfolio)
    book.last_run_date = date
    return portfolio
