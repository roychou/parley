"""
Forward session runner — one scheduled paper-trading step, end to end.

Ties the pieces together for a single live decision date: screen the universe (fresh
filers ∪ holdings), get a Decision per candidate from the injected decision provider,
then mark-to-market + execute against the persistent PaperBook. Vendor-agnostic — the
decision provider and the current-price function are injected, so the IBKR adapters
(or any source) drop into these seams without touching this loop.

This is the forward analog of one iteration of the backtest replay loop, but stateful
across runs (loads/saves the PaperBook). A scheduler (cron / launchd) calls this weekly.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from src.backtest.costs import CostModel
from src.backtest.screen import FilingDatesFn, select_candidates
from src.backtest.strategies import _is_fatal_error
from src.forward.paper import PaperBook, run_forward_step
from src.risk import RiskConfig
from src.schemas import Decision

logger = logging.getLogger(__name__)

DecisionProvider = Callable[[str, str], Awaitable[Decision | None]]
CurrentPrice = Callable[[str], float | None]
Volatility = Callable[[str], float | None]   # ticker -> annualized vol (for risk sizing)


def _window_start(as_of: str, lookback_days: int) -> str:
    y, m, d = (int(x) for x in as_of.split("-"))
    return (date(y, m, d) - timedelta(days=lookback_days)).isoformat()


async def produce_decisions(
    candidates: list[str], as_of: str, decision_provider: DecisionProvider,
) -> tuple[list[Decision], list[str]]:
    """Run the decision provider over each candidate; return (decisions, skipped).

    A fatal error (billing/auth/budget cap) propagates — never masked. A per-ticker
    data error (or a None decision) just skips that name. Shared by the simulated and
    broker-execution paths so both decide identically and only differ in execution.
    """
    decisions: list[Decision] = []
    skipped: list[str] = []
    for ticker in candidates:
        try:
            decision = await decision_provider(ticker, as_of)
        except Exception as e:
            if _is_fatal_error(e):  # billing/auth/budget cap — abort, don't mask
                raise
            logger.warning(f"skipping {ticker}: {type(e).__name__}: {e}")
            decision = None
        if decision is not None:
            decisions.append(decision)
        else:
            skipped.append(ticker)
    return decisions, skipped


async def run_forward_session(
    book: PaperBook,
    as_of: str,
    eligible_universe: list[str],
    *,
    decision_provider: DecisionProvider,
    current_price: CurrentPrice,
    filing_dates_fn: FilingDatesFn | None = None,
    screen_lookback_days: int = 7,
    cost_model: CostModel | None = None,
    dividends: dict[str, float] | None = None,
    stop_loss_pct: float | None = -0.20,
    risk_config: RiskConfig | None = None,
    volatility: Volatility | None = None,
    candidates: list[str] | None = None,
) -> dict:
    """Run one forward paper-trading session and persist the book.

    Candidates = names that filed in the trailing window ∪ current holdings (or the
    whole eligible universe when no filing_dates_fn is given). A candidate that can't
    be decided (provider returns None — missing data) is simply skipped. Returns a
    small summary dict for logging/audit.
    """
    held = list(book.positions)
    if candidates is None:  # caller may pre-screen (to refresh only these names)
        if filing_dates_fn is not None:
            candidates = select_candidates(
                eligible_universe, held, _window_start(as_of, screen_lookback_days), as_of,
                filing_dates_fn,
            )
        else:
            candidates = sorted(set(eligible_universe) | set(held))

    decisions, skipped = await produce_decisions(candidates, as_of, decision_provider)

    # Prices for MTM + execution: everything held or freshly decided. Drop names with
    # no current price (can't mark or trade them this session).
    wanted = set(held) | {d.ticker for d in decisions}
    prices = {t: current_price(t) for t in wanted}
    prices = {t: p for t, p in prices.items() if p is not None}

    # Risk sizing (when configured) needs per-BUY volatility from the injected source.
    vols = None
    if risk_config is not None and volatility is not None:
        vols = {d.ticker: volatility(d.ticker) for d in decisions if d.direction == "BUY"}

    run_forward_step(
        book, as_of, prices, decisions,
        cost_model=cost_model, dividends=dividends, stop_loss_pct=stop_loss_pct,
        risk_config=risk_config, vols=vols,
    )
    book.save()

    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.direction] = counts.get(d.direction, 0) + 1
    summary = {
        "as_of": as_of,
        "candidates": len(candidates),
        "decided": len(decisions),
        "directions": counts,
        "skipped": skipped,
        "open_positions": len(book.positions),
        "equity": book.equity_curve[-1]["total_value"] if book.equity_curve else book.cash,
    }
    logger.info(f"forward session {as_of}: {summary}")
    return summary
