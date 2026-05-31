"""Dividend (total-return) crediting: Portfolio + end-to-end replay."""
import pytest

from src.backtest.portfolio import Portfolio
from src.backtest.replay import BacktestConfig, run_backtest
from src.backtest.strategies import SPYHoldStrategy

_DATES = ["2026-01-09", "2026-01-16", "2026-01-23"]


def _history(closes: dict[str, list[float]]):
    return {
        t: {d: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for d, c in zip(_DATES, cs)}
        for t, cs in closes.items()
    }


# ---- Portfolio.apply_dividends ------------------------------------------
def test_apply_dividends_credits_held_positions_only():
    p = Portfolio(initial_cash=100_000.0)
    p.open("AAA", "2026-01-09", price=100.0, dollars=10_000.0)  # 100 shares
    cash_before = p.cash

    # $0.50/share dividend on AAA (held) and BBB (not held).
    credited = p.apply_dividends({"AAA": 0.50, "BBB": 1.00})

    assert credited == pytest.approx(50.0)            # 100 shares * $0.50
    assert p.cash == pytest.approx(cash_before + 50.0)
    assert p.dividends_received == pytest.approx(50.0)


def test_apply_dividends_noop_without_holdings():
    p = Portfolio(initial_cash=100_000.0)
    assert p.apply_dividends({"AAA": 1.0}) == 0.0
    assert p.dividends_received == 0.0


# ---- end-to-end: dividends raise total return through the replay ---------
@pytest.mark.asyncio
async def test_dividends_increase_total_return_end_to_end():
    history = _history({"SPY": [400.0, 400.0, 400.0]})  # flat price: isolate dividends

    def price_loader(t):
        return history[t]

    def fundamentals_loader(t, d):
        return None

    # A $4/share ex-dividend on the middle date.
    def dividends_loader(t):
        return {"2026-01-16": 4.0} if t == "SPY" else {}

    def cfg():
        return BacktestConfig(
            universe=["SPY"], decision_dates=_DATES,
            strategies=[SPYHoldStrategy()], stop_loss_pct=None,
        )

    no_div = await run_backtest(cfg(), price_loader, fundamentals_loader)
    with_div = await run_backtest(cfg(), price_loader, fundamentals_loader, dividends_loader)

    end_no = no_div.outcomes["spy_hold"].portfolio.equity_curve[-1].total_value
    end_with = with_div.outcomes["spy_hold"].portfolio.equity_curve[-1].total_value
    assert end_with > end_no  # the dividend cash lifts total return on a flat price
    assert with_div.outcomes["spy_hold"].portfolio.dividends_received > 0
