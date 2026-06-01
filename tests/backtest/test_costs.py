"""Transaction-cost model + its effect on the Portfolio."""
import pytest

from src.backtest.costs import CostModel
from src.backtest.portfolio import Portfolio
from src.backtest.replay import BacktestConfig, run_backtest
from src.backtest.strategies import SPYHoldStrategy

_DATES = ["2026-01-09", "2026-01-16", "2026-01-23"]


def _price_history():
    closes = {"SPY": [400.0, 404.0, 408.0]}
    return {
        t: {d: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for d, c in zip(_DATES, cs)}
        for t, cs in closes.items()
    }


# ---- CostModel unit ------------------------------------------------------
def test_frictionless_default():
    c = CostModel()
    assert c.is_frictionless
    assert c.fill_price(100.0, "BUY") == 100.0
    assert c.fill_price(100.0, "SELL") == 100.0
    assert c.commission(10_000.0, 100.0) == 0.0


def test_slippage_is_adverse():
    c = CostModel(slippage_bps=10.0)  # 0.10%
    assert c.fill_price(100.0, "BUY") == pytest.approx(100.10)   # buy pays up
    assert c.fill_price(100.0, "SELL") == pytest.approx(99.90)   # sell receives less
    assert not c.is_frictionless


def test_commission_bps_and_per_share_and_min():
    c = CostModel(commission_bps=2.0, commission_per_share=0.005, min_commission=1.0)
    # 10_000 notional @ $100 = 100 shares: 10_000*0.0002 + 100*0.005 = 2.0 + 0.5 = 2.5
    assert c.commission(10_000.0, 100.0) == pytest.approx(2.5)
    # tiny trade hits the floor
    assert c.commission(100.0, 100.0) == pytest.approx(1.0)
    # no trade, no charge
    assert c.commission(0.0, 100.0) == 0.0


def test_ibkr_singapore_fixed_preset():
    c = CostModel.ibkr_singapore_fixed()
    assert c.slippage_bps == 5.0
    # $10k position @ $100 = 100 shares: 100*$0.005 = $0.50, floored to $1 min
    assert c.commission(10_000.0, 100.0) == pytest.approx(1.0)
    # large position: per-share dominates, above the $1 floor, below the 1% cap
    # $100k @ $200 = 500 shares * $0.005 = $2.50
    assert c.commission(100_000.0, 200.0) == pytest.approx(2.50)
    # tiny order: 1% cap overrides the $1 floor ($30 trade -> $0.30, not $1)
    assert c.commission(30.0, 100.0) == pytest.approx(0.30)


# ---- Portfolio integration ----------------------------------------------
def test_zero_cost_portfolio_unchanged():
    """A frictionless Portfolio: round-trip at the same price nets exactly zero."""
    p = Portfolio(initial_cash=100_000.0)
    p.open("AAA", "2026-01-01", price=100.0, dollars=10_000.0)
    assert p.cash == 90_000.0
    p.close("AAA", "2026-01-08", price=100.0, reason="SELL_signal")
    assert p.cash == pytest.approx(100_000.0)
    assert p.closed_trades[0].realized_pnl_pct == pytest.approx(0.0)


def test_costs_make_a_flat_roundtrip_lose_money():
    """With slippage + commission, buying and selling at the same quoted price is a
    loss — the friction is the whole P&L."""
    costs = CostModel(slippage_bps=10.0, commission_bps=2.0)
    p = Portfolio(initial_cash=100_000.0, cost_model=costs)

    p.open("AAA", "2026-01-01", price=100.0, dollars=10_000.0)
    # entry fill 100.10; entry commission = 10_000 * 0.0002 = 2.0
    pos = p.positions["AAA"]
    assert pos.entry_price == pytest.approx(100.10)
    assert pos.cost_at_entry == pytest.approx(2.0)
    assert p.cash == pytest.approx(100_000.0 - 10_000.0 - 2.0)

    p.close("AAA", "2026-01-08", price=100.0, reason="SELL_signal")
    # quoted price unchanged, but slippage + both commissions => net loss
    trade = p.closed_trades[0]
    assert trade.realized_pnl_pct < 0
    assert p.cash < 100_000.0
    # round-trip cost ≈ 20bps slippage + ~4bps commission on ~10k ≈ $24
    assert (100_000.0 - p.cash) == pytest.approx(24.0, abs=0.2)


def test_all_in_open_shrinks_to_fit_commission():
    """An all-in open (notional == cash) where commission would tip the outlay over
    cash is shrunk to deploy all investable cash net of fees, NOT rejected. Rejecting
    it silently broke the fully-invested SPY benchmark under realistic commissions.
    The no-overdraw invariant still holds: cash never goes negative."""
    costs = CostModel(commission_bps=10.0)
    p = Portfolio(initial_cash=10_000.0, cost_model=costs)
    assert p.open("AAA", "2026-01-01", price=100.0, dollars=10_000.0) is True
    assert "AAA" in p.positions
    assert 0.0 <= p.cash < 20.0  # all but the commission sliver deployed; never overdrawn


# ---- end-to-end: costs flow through the replay loop ----------------------
@pytest.mark.asyncio
async def test_cost_model_threads_through_replay():
    """BacktestConfig.cost_model reaches each strategy's Portfolio: the costed
    SPY-hold ends below the frictionless one over the same rising path."""
    history = _price_history()

    def price_loader(t):
        return history[t]

    def fundamentals_loader(t, d):
        return None

    def final_value(cost_model):
        config = BacktestConfig(
            universe=["SPY"], decision_dates=_DATES,
            strategies=[SPYHoldStrategy()], stop_loss_pct=None,
            cost_model=cost_model,
        )
        return config, price_loader, fundamentals_loader

    free_cfg, pl, fl = final_value(None)
    costed_cfg, _, _ = final_value(CostModel(slippage_bps=20.0, commission_bps=5.0))

    free = await run_backtest(free_cfg, pl, fl)
    costed = await run_backtest(costed_cfg, pl, fl)

    free_end = free.outcomes["spy_hold"].portfolio.equity_curve[-1].total_value
    costed_end = costed.outcomes["spy_hold"].portfolio.equity_curve[-1].total_value
    assert costed_end < free_end  # friction is subtracted from real money
