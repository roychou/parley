"""
IBKR paper-account execution adapter (Level B) — place orders, read real state.

The forward clock can run two ways: simulated (the PaperBook marks fills with the cost
model) or broker-executed (this module places orders against the IBKR *paper* account
and reads positions/equity back — the truest dress rehearsal short of live money).

SAFETY — this is the only code in the project that transmits orders:
- _assert_paper(): every managed account id must be a paper id (starts 'DU'); it
  refuses to act against a live account, and is re-checked immediately before sending.
- transmit defaults to False everywhere: plans are previewed (logged), nothing is sent.
  Real placement needs transmit=True AND IB Gateway's "Read-Only API" turned OFF.
- Market orders only; OPENs are sized from the risk layer's target weight × the REAL
  account equity, CLOSEs sell exactly the shares actually held. Whole shares only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ib_async import IB, MarketOrder, Stock

from src.backtest.strategies import Action, MultiAgentStrategy
from src.data.sectors import sector_of

logger = logging.getLogger(__name__)

PAPER_ACCT_PREFIX = "DU"  # IBKR paper accounts; live individual accounts start 'U'


class NotPaperAccountError(RuntimeError):
    """Raised when the connected IBKR account is not a paper account."""


def _assert_paper(ib: IB) -> str:
    """Return the connected account id, raising unless EVERY managed account is paper."""
    accts = ib.managedAccounts()
    if not accts:
        raise NotPaperAccountError("no managed accounts on the IBKR connection")
    for a in accts:
        if not a.startswith(PAPER_ACCT_PREFIX):
            raise NotPaperAccountError(
                f"refusing to trade: account {a!r} is not a paper account "
                f"(paper ids start {PAPER_ACCT_PREFIX!r})"
            )
    return accts[0]


@dataclass(frozen=True)
class AccountState:
    account: str
    equity: float                 # NetLiquidation
    cash: float                   # TotalCashValue
    positions: dict[str, float]   # ticker -> shares held (signed)
    avg_cost: dict[str, float]    # ticker -> average cost basis


def account_state(ib: IB) -> AccountState:
    """Read the (paper) account: equity, cash, and current share positions."""
    acct = _assert_paper(ib)

    def _f(tag: str) -> float:
        for v in ib.accountValues():
            if v.tag == tag and v.account in (acct, ""):
                try:
                    return float(v.value)
                except ValueError:
                    return float("nan")
        return float("nan")

    held = [p for p in ib.positions() if p.position]
    positions = {p.contract.symbol: float(p.position) for p in held}
    avg_cost = {p.contract.symbol: float(p.avgCost) for p in held}
    return AccountState(
        account=acct, equity=_f("NetLiquidation"), cash=_f("TotalCashValue"),
        positions=positions, avg_cost=avg_cost,
    )


@dataclass(frozen=True)
class OrderPlan:
    ticker: str
    side: str       # "BUY" | "SELL"
    quantity: int   # whole shares
    reason: str


def plan_orders(
    actions: list[Action],
    prices: dict[str, float],
    equity: float,
    held_shares: dict[str, float],
) -> list[OrderPlan]:
    """Translate broker-agnostic actions into whole-share market-order plans, sized
    against the real account. CLOSE sells exactly the shares held; OPEN buys
    floor(equity × position_size_pct / price). Pure — no IB calls. Skips a CLOSE of an
    unheld name, an OPEN with no price, a non-positive quantity, or (defensively) any
    single order whose notional would exceed account equity."""
    plans: list[OrderPlan] = []
    for a in actions:
        if a.kind == "CLOSE":
            qty = int(abs(held_shares.get(a.ticker, 0.0)))
            if qty > 0:
                plans.append(OrderPlan(a.ticker, "SELL", qty, a.reason or "close"))
        elif a.kind == "OPEN":
            price = prices.get(a.ticker)
            if not price or price <= 0:
                logger.warning(f"skip OPEN {a.ticker}: no usable price")
                continue
            if not 0.0 < a.position_size_pct <= 1.0:  # risk layer bounds this; guard anyway
                logger.warning(f"skip OPEN {a.ticker}: bad size {a.position_size_pct}")
                continue
            qty = int((equity * a.position_size_pct) // price)  # floor to whole shares
            if qty > 0 and qty * price <= equity:
                plans.append(OrderPlan(a.ticker, "BUY", qty, "open"))
    return plans


async def _await_done(trade, timeout: float) -> None:
    async def _loop() -> None:
        while not trade.isDone():
            await asyncio.sleep(0.3)
    await asyncio.wait_for(_loop(), timeout=timeout)


async def execute_orders(
    ib: IB, plans: list[OrderPlan], *, transmit: bool = False, fill_timeout: float = 30.0,
) -> list[dict]:
    """Place (or preview) the planned market orders against the paper account.

    transmit=False (default): preview only — logs each order, sends nothing.
    transmit=True: places MarketOrders and waits up to fill_timeout for each fill.
    Requires Gateway's Read-Only API OFF. The paper guard is re-checked before sending.
    """
    if transmit:
        _assert_paper(ib)  # re-assert at the last moment before any order leaves
    results: list[dict] = []
    for p in plans:
        if not transmit:
            logger.info(f"[PREVIEW] {p.side} {p.quantity} {p.ticker} ({p.reason})")
            results.append({"ticker": p.ticker, "side": p.side, "qty": p.quantity,
                            "status": "preview", "reason": p.reason})
            continue
        contract = Stock(p.ticker, "SMART", "USD")
        await ib.qualifyContractsAsync(contract)
        # Set TIF explicitly to DAY. Without it, IBKR's order preset overrides the (empty)
        # TIF and emits warning 10349, which ib_async surfaces as a transient 'Cancelled'
        # status — the order still fills, but it confuses fill monitoring. An explicit DAY
        # matches the preset, so there's nothing to override and no spurious cancel.
        order = MarketOrder(p.side, p.quantity)
        order.tif = "DAY"
        trade = ib.placeOrder(contract, order)
        try:
            await _await_done(trade, fill_timeout)
        except TimeoutError:
            logger.warning(f"order {p.side} {p.quantity} {p.ticker}: not done in {fill_timeout}s")
        st = trade.orderStatus
        fill = st.avgFillPrice if st.filled else None
        logger.info(f"{p.side} {p.quantity} {p.ticker}: {st.status} filled={st.filled} @ {fill}")
        results.append({"ticker": p.ticker, "side": p.side, "qty": p.quantity,
                        "status": st.status, "filled": st.filled, "avg_fill": fill})
    return results


# ==========================================
# FULL BROKER PATH (read -> size -> plan -> preview/transmit)
# ==========================================


def _portfolio_from_account(state: AccountState, equity_curve: list, cost_model=None):
    """Reconstruct a backtest Portfolio that mirrors the real account, so the existing
    risk layer (build_actions) sizes against actual positions/equity. Only membership
    (held/can_open) and the equity curve (drawdown governor) are used by build_actions."""
    from src.backtest.portfolio import EquitySnapshot, Portfolio, Position
    p = Portfolio(initial_cash=state.equity, cost_model=cost_model)
    p.cash = state.cash
    p.positions = {
        t: Position(ticker=t, entry_date="", entry_price=state.avg_cost.get(t) or 1.0,
                    dollars_at_entry=shares * (state.avg_cost.get(t) or 0.0), cost_at_entry=0.0)
        for t, shares in state.positions.items()
    }
    p.equity_curve = [EquitySnapshot(**s) if isinstance(s, dict) else s for s in equity_curve]
    return p


async def broker_rebalance(
    ib: IB, decisions, prices: dict[str, float], *,
    vols=None, risk_config=None, equity_curve=None, cost_model=None, transmit: bool = False,
) -> dict:
    """One broker rebalance: read the paper account, size the decisions against it with
    the same risk layer as the backtest, plan whole-share market orders, and preview
    (transmit=False, default) or transmit them. `equity_curve` (the PaperBook's, as
    dicts) feeds the drawdown governor. Returns the account, the plan, and results."""
    state = account_state(ib)
    portfolio = _portfolio_from_account(state, equity_curve or [], cost_model)
    translator = MultiAgentStrategy(
        decision_provider=None, risk_config=risk_config, sector_map_fn=sector_of,
    )
    actions = translator.build_actions(list(decisions), portfolio, vols=vols)
    plans = plan_orders(actions, prices, state.equity, state.positions)
    results = await execute_orders(ib, plans, transmit=transmit)
    return {
        "account": state.account, "equity": state.equity, "transmit": transmit,
        "plans": [(p.side, p.quantity, p.ticker) for p in plans], "results": results,
    }
