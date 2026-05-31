"""
Transaction-cost model for the backtest.

Applied at every fill so that backtest P&L reflects what a real account would
keep, not a frictionless idealization. Two mechanisms:

- **slippage** (`slippage_bps`): the fill price moves *adversely* to the trade —
  buys fill higher, sells fill lower. Models half the bid/ask spread plus
  immediate slippage between the decision and the execution.
- **commission**: a cash charge per fill — bps of notional and/or per-share, with
  an optional per-order minimum.

Defaults are **zero** (frictionless), so a Portfolio without a cost model behaves
exactly as before (existing tests unchanged). Realistic values are supplied by the
runner and should be *swept* — whether the strategy's edge survives plausible costs
is the central question of productization Phase 0 (GATE 0).

Deliberately **not** modeled here: **market impact** (the cost of size moving the
price), which is size- and ADV-dependent and belongs to the capacity analysis
(productization.md Phase 0.5). This model is size-agnostic — correct for a personal
account small relative to large-cap ADV, optimistic at size.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.0        # % of notional, in basis points (1 bp = 0.01%)
    commission_per_share: float = 0.0  # cash per share
    min_commission: float = 0.0        # per-order floor
    slippage_bps: float = 0.0          # adverse price move at fill, in basis points

    def fill_price(self, price: float, side: str) -> float:
        """Adverse fill: a BUY pays up, a SELL receives less. `side` in {BUY, SELL}."""
        adj = self.slippage_bps / 10_000.0
        return price * (1.0 + adj) if side == "BUY" else price * (1.0 - adj)

    def commission(self, notional: float, fill_price: float) -> float:
        """Cash commission for a fill of `notional` dollars at `fill_price`."""
        if notional <= 0 or fill_price <= 0:
            return 0.0
        shares = notional / fill_price
        charge = notional * (self.commission_bps / 10_000.0) + shares * self.commission_per_share
        return max(charge, self.min_commission)

    @property
    def is_frictionless(self) -> bool:
        return (
            self.commission_bps == 0.0
            and self.commission_per_share == 0.0
            and self.min_commission == 0.0
            and self.slippage_bps == 0.0
        )

    def describe(self) -> str:
        if self.is_frictionless:
            return "frictionless (no costs)"
        return (
            f"slippage={self.slippage_bps}bps, commission={self.commission_bps}bps"
            f"+${self.commission_per_share}/sh (min ${self.min_commission})"
        )
