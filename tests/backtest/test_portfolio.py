import pytest

from src.backtest.portfolio import Portfolio


# ==========================================
# OPEN
# ==========================================


def test_open_succeeds_when_can_open():
    p = Portfolio(initial_cash=100_000, max_positions=10)
    ok = p.open("AAPL", "2026-01-09", price=150.0, dollars=10_000)
    assert ok is True
    assert p.cash == 90_000
    assert "AAPL" in p.positions
    pos = p.positions["AAPL"]
    assert pos.entry_price == 150.0
    assert pos.dollars_at_entry == 10_000


def test_open_rejected_when_already_held():
    p = Portfolio(initial_cash=100_000, max_positions=10)
    p.open("AAPL", "2026-01-09", price=150.0, dollars=10_000)
    ok = p.open("AAPL", "2026-01-16", price=155.0, dollars=10_000)
    assert ok is False
    assert p.cash == 90_000  # second open had no effect


def test_open_rejected_when_at_max_positions():
    p = Portfolio(initial_cash=100_000, max_positions=2)
    p.open("AAA", "2026-01-09", price=10.0, dollars=1_000)
    p.open("BBB", "2026-01-09", price=10.0, dollars=1_000)
    ok = p.open("CCC", "2026-01-09", price=10.0, dollars=1_000)
    assert ok is False
    assert "CCC" not in p.positions
    assert p.cash == 98_000


def test_open_rejected_when_insufficient_cash():
    p = Portfolio(initial_cash=5_000, max_positions=10)
    ok = p.open("AAPL", "2026-01-09", price=150.0, dollars=10_000)
    assert ok is False
    assert p.cash == 5_000


def test_open_rejected_for_non_positive_inputs():
    p = Portfolio(initial_cash=100_000)
    assert p.open("AAPL", "2026-01-09", price=0, dollars=1_000) is False
    assert p.open("AAPL", "2026-01-09", price=150.0, dollars=0) is False
    assert p.open("AAPL", "2026-01-09", price=-1, dollars=1_000) is False


# ==========================================
# CLOSE
# ==========================================


def test_close_realized_pnl_positive():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)
    ok = p.close("AAPL", "2026-02-09", price=110.0, reason="SELL_signal")

    assert ok is True
    assert "AAPL" not in p.positions
    assert len(p.closed_trades) == 1
    trade = p.closed_trades[0]
    assert trade.realized_pnl_pct == pytest.approx(0.10)
    assert trade.exit_reason == "SELL_signal"
    # cash: started 100k, paid 10k for position, recovered 11k on close = 101k
    assert p.cash == pytest.approx(101_000)


def test_close_realized_pnl_negative():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)
    p.close("AAPL", "2026-02-09", price=80.0, reason="SELL_signal")

    trade = p.closed_trades[0]
    assert trade.realized_pnl_pct == pytest.approx(-0.20)
    assert p.cash == pytest.approx(98_000)


def test_close_not_held_returns_false():
    p = Portfolio(initial_cash=100_000)
    ok = p.close("AAPL", "2026-02-09", price=100.0, reason="SELL_signal")
    assert ok is False
    assert len(p.closed_trades) == 0


# ==========================================
# MARK_TO_MARKET
# ==========================================


def test_mtm_appends_equity_snapshot():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)

    p.mark_to_market(prices={"AAPL": 110.0}, date="2026-01-16", stop_loss_pct=None)

    assert len(p.equity_curve) == 1
    snap = p.equity_curve[0]
    assert snap.date == "2026-01-16"
    assert snap.cash == 90_000
    # AAPL marked up 10% → 10_000 * 1.10 = 11_000
    assert snap.positions_value == pytest.approx(11_000)
    assert snap.total_value == pytest.approx(101_000)


def test_mtm_triggers_stop_loss_at_threshold():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)

    stopped = p.mark_to_market(prices={"AAPL": 80.0}, date="2026-01-16", stop_loss_pct=-0.20)

    assert stopped == ["AAPL"]
    assert "AAPL" not in p.positions
    assert len(p.closed_trades) == 1
    assert p.closed_trades[0].exit_reason == "stop_loss"


def test_mtm_does_not_trigger_stop_loss_above_threshold():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)

    stopped = p.mark_to_market(prices={"AAPL": 85.0}, date="2026-01-16", stop_loss_pct=-0.20)

    assert stopped == []
    assert "AAPL" in p.positions
    assert len(p.closed_trades) == 0


def test_mtm_with_stop_loss_none_does_not_trigger():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)

    # Position is down 50% but stop_loss is disabled
    stopped = p.mark_to_market(prices={"AAPL": 50.0}, date="2026-01-16", stop_loss_pct=None)

    assert stopped == []
    assert "AAPL" in p.positions


def test_mtm_handles_missing_price_for_held_ticker():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)
    p.open("MSFT", "2026-01-09", price=200.0, dollars=20_000)

    # Only AAPL has a price; MSFT falls back to entry_price (no mark)
    p.mark_to_market(prices={"AAPL": 110.0}, date="2026-01-16", stop_loss_pct=None)

    snap = p.equity_curve[0]
    # AAPL value: 11_000. MSFT value: 20_000 (unmarked). Cash: 70_000. Total: 101_000.
    assert snap.positions_value == pytest.approx(31_000)
    assert snap.total_value == pytest.approx(101_000)


# ==========================================
# CLOSE_ALL
# ==========================================


def test_close_all_closes_every_open_position():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)
    p.open("MSFT", "2026-01-09", price=200.0, dollars=20_000)

    p.close_all(prices={"AAPL": 110.0, "MSFT": 210.0}, date="2026-06-30")

    assert len(p.positions) == 0
    assert len(p.closed_trades) == 2
    for trade in p.closed_trades:
        assert trade.exit_reason == "end_of_backtest"


def test_close_all_uses_entry_price_when_price_missing():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)

    p.close_all(prices={}, date="2026-06-30")  # no prices available

    assert len(p.positions) == 0
    assert len(p.closed_trades) == 1
    # closed at entry price → zero realized P&L
    assert p.closed_trades[0].realized_pnl_pct == pytest.approx(0.0)


# ==========================================
# QUERIES
# ==========================================


def test_total_value_with_open_positions():
    p = Portfolio(initial_cash=100_000)
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)

    # Position marked up 20% → 12_000; cash = 90_000; total = 102_000
    val = p.total_value(prices={"AAPL": 120.0})
    assert val == pytest.approx(102_000)


def test_can_open_logic():
    p = Portfolio(initial_cash=100_000, max_positions=2)
    assert p.can_open("AAPL") is True
    p.open("AAPL", "2026-01-09", price=100.0, dollars=10_000)
    assert p.can_open("AAPL") is False  # already held
    p.open("MSFT", "2026-01-09", price=200.0, dollars=20_000)
    assert p.can_open("GOOGL") is False  # at max
