"""Walk-forward / out-of-sample split + per-segment metrics."""
import pytest

from src.backtest.portfolio import EquitySnapshot, Trade
from src.backtest.replay import BacktestConfig, run_backtest
from src.backtest.strategies import SPYHoldStrategy
from src.backtest.validation import choose_split_date, split_by_date, walk_forward_metrics


def _snap(date, total):
    return EquitySnapshot(date=date, cash=total, positions_value=0.0, total_value=total)


def _trade(entry_date, pnl):
    return Trade(
        ticker="AAA", entry_date=entry_date, entry_price=100.0,
        exit_date=entry_date, exit_price=100.0 * (1 + pnl), exit_reason="SELL_signal",
        dollars_at_entry=1000.0, realized_pnl_pct=pnl,
    )


def test_split_by_date_partitions_equity_and_trades():
    equity = [_snap(d, 100.0) for d in ["2026-01-01", "2026-01-15", "2026-02-01", "2026-02-15"]]
    trades = [_trade("2026-01-10", 0.05), _trade("2026-02-10", -0.02)]

    (is_eq, is_tr), (oos_eq, oos_tr) = split_by_date(equity, trades, "2026-01-31")

    assert [s.date for s in is_eq] == ["2026-01-01", "2026-01-15"]
    assert [s.date for s in oos_eq] == ["2026-02-01", "2026-02-15"]
    assert [t.entry_date for t in is_tr] == ["2026-01-10"]   # by entry_date (decision)
    assert [t.entry_date for t in oos_tr] == ["2026-02-10"]


def test_choose_split_date_by_fraction():
    dates = ["2026-01-01", "2026-01-08", "2026-01-15", "2026-01-22", "2026-01-29"]
    # 0.6 of 5 = 3 -> index 2 -> the 3rd date is the last in-sample date
    assert choose_split_date(dates, 0.6) == "2026-01-15"


@pytest.mark.asyncio
async def test_walk_forward_metrics_segments_a_real_run():
    dates = ["2026-01-09", "2026-01-16", "2026-01-23", "2026-01-30"]
    history = {
        "SPY": {d: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
                for d, c in zip(dates, [400.0, 404.0, 408.0, 412.0])}
    }

    def price_loader(t):
        return history[t]

    def fundamentals_loader(t, d):
        return None

    config = BacktestConfig(
        universe=["SPY"], decision_dates=dates,
        strategies=[SPYHoldStrategy()], stop_loss_pct=None,
    )
    result = await run_backtest(config, price_loader, fundamentals_loader)

    report = walk_forward_metrics(result, split_date="2026-01-16")
    segs = report["spy_hold"]
    assert [s.label for s in segs] == ["in-sample", "out-of-sample"]
    # in-sample ends on/before the split; out-of-sample starts after it
    assert segs[0].end <= "2026-01-16" < segs[1].start
