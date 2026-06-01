
import pytest

from src.backtest.metrics import (
    annualized_return,
    compute_metrics,
    hit_rate,
    max_drawdown,
    sharpe_ratio,
    total_return,
)
from src.backtest.portfolio import EquitySnapshot, Trade

# ==========================================
# TOTAL_RETURN
# ==========================================


def test_total_return_simple():
    curve = [
        EquitySnapshot(date="2026-01-09", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-06-30", cash=110_000, positions_value=0, total_value=110_000),
    ]
    assert total_return(curve) == pytest.approx(0.10)


def test_total_return_negative():
    curve = [
        EquitySnapshot(date="2026-01-09", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-06-30", cash=85_000, positions_value=0, total_value=85_000),
    ]
    assert total_return(curve) == pytest.approx(-0.15)


def test_total_return_empty_curve_returns_zero():
    assert total_return([]) == 0.0


def test_total_return_single_point():
    curve = [EquitySnapshot(date="2026-01-09", cash=100_000, positions_value=0, total_value=100_000)]
    # Single point → start == end → 0
    assert total_return(curve) == 0.0


# ==========================================
# ANNUALIZED_RETURN
# ==========================================


def test_annualized_return_one_year_matches_total_return():
    curve = [
        EquitySnapshot(date="2025-06-30", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-06-30", cash=110_000, positions_value=0, total_value=110_000),
    ]
    # ~1 year window → CAGR ≈ total return
    assert annualized_return(curve) == pytest.approx(0.10, abs=0.001)


def test_annualized_return_half_year_doubles():
    curve = [
        EquitySnapshot(date="2026-01-01", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-07-02", cash=110_000, positions_value=0, total_value=110_000),
    ]
    # ~0.5 year window, 10% return → annualized ≈ (1.10)^2 - 1 = 0.21
    result = annualized_return(curve)
    assert result == pytest.approx(0.21, abs=0.01)


def test_annualized_return_insufficient_data():
    assert annualized_return([]) == 0.0
    single = [EquitySnapshot(date="2026-01-09", cash=100_000, positions_value=0, total_value=100_000)]
    assert annualized_return(single) == 0.0


# ==========================================
# SHARPE_RATIO
# ==========================================


def test_sharpe_constant_returns_returns_zero():
    # Doubling values produce bit-exact identical returns (+100% each step) → zero volatility → Sharpe = 0
    values = [100_000, 200_000, 400_000, 800_000]
    curve = [
        EquitySnapshot(date=f"2026-01-{i:02d}", cash=v, positions_value=0, total_value=v)
        for i, v in enumerate(values, start=1)
    ]
    assert sharpe_ratio(curve) == 0.0


def test_sharpe_positive_excess_with_volatility():
    # Hand-constructed: mean positive, some variance
    values = [100_000, 102_000, 101_000, 104_000, 103_000, 106_000]
    curve = [
        EquitySnapshot(date=f"2026-01-{i:02d}", cash=v, positions_value=0, total_value=v)
        for i, v in enumerate(values, start=1)
    ]
    s = sharpe_ratio(curve, periods_per_year=52, risk_free_rate=0.0)
    # Expected: positive Sharpe (positive mean return, finite stdev)
    assert s > 0


def test_sharpe_insufficient_data():
    assert sharpe_ratio([]) == 0.0
    single = [EquitySnapshot(date="2026-01-09", cash=100_000, positions_value=0, total_value=100_000)]
    assert sharpe_ratio(single) == 0.0


def test_sharpe_risk_free_rate_lowers_result():
    # Same volatile curve evaluated at two risk-free rates. Higher rf → lower Sharpe (excess shrinks).
    values = [100_000, 102_000, 101_000, 104_000, 103_000, 106_000]
    curve = [
        EquitySnapshot(date=f"2026-01-{i:02d}", cash=v, positions_value=0, total_value=v)
        for i, v in enumerate(values, start=1)
    ]
    sharpe_at_zero = sharpe_ratio(curve, periods_per_year=52, risk_free_rate=0.0)
    sharpe_at_high_rf = sharpe_ratio(curve, periods_per_year=52, risk_free_rate=0.50)
    assert sharpe_at_zero > sharpe_at_high_rf


# ==========================================
# MAX_DRAWDOWN
# ==========================================


def test_max_drawdown_monotonically_increasing_is_zero():
    curve = [
        EquitySnapshot(date=f"2026-01-{i:02d}", cash=v, positions_value=0, total_value=v)
        for i, v in enumerate([100_000, 105_000, 110_000, 120_000], start=1)
    ]
    assert max_drawdown(curve) == 0.0


def test_max_drawdown_simple_peak_trough():
    # Peak 110_000, trough 88_000 → drawdown = (88-110)/110 = -0.20
    curve = [
        EquitySnapshot(date=f"2026-01-{i:02d}", cash=v, positions_value=0, total_value=v)
        for i, v in enumerate([100_000, 110_000, 88_000, 95_000], start=1)
    ]
    assert max_drawdown(curve) == pytest.approx(-0.20, abs=0.001)


def test_max_drawdown_multiple_drawdowns_takes_worst():
    # First DD: 105 -> 95 = -9.5%. Second DD: 120 -> 90 = -25%.
    curve = [
        EquitySnapshot(date=f"2026-01-{i:02d}", cash=v, positions_value=0, total_value=v)
        for i, v in enumerate([100_000, 105_000, 95_000, 120_000, 90_000, 100_000], start=1)
    ]
    assert max_drawdown(curve) == pytest.approx(-0.25, abs=0.001)


def test_max_drawdown_empty():
    assert max_drawdown([]) == 0.0


# ==========================================
# HIT_RATE
# ==========================================


def _trade(pnl: float, reason: str = "SELL_signal") -> Trade:
    return Trade(
        ticker="X",
        entry_date="2026-01-09",
        entry_price=100.0,
        exit_date="2026-02-09",
        exit_price=100.0 * (1 + pnl),
        exit_reason=reason,
        dollars_at_entry=10_000,
        realized_pnl_pct=pnl,
    )


def test_hit_rate_all_winners():
    trades = [_trade(0.10), _trade(0.05), _trade(0.20)]
    assert hit_rate(trades) == 1.0


def test_hit_rate_all_losers():
    trades = [_trade(-0.10), _trade(-0.05), _trade(-0.20)]
    assert hit_rate(trades) == 0.0


def test_hit_rate_mixed():
    trades = [_trade(0.10), _trade(-0.05), _trade(0.20), _trade(-0.15)]
    assert hit_rate(trades) == 0.5


def test_hit_rate_zero_pnl_not_a_winner():
    # Strict ">0" — exactly-zero is not counted as a win
    trades = [_trade(0.0), _trade(0.10)]
    assert hit_rate(trades) == 0.5


def test_hit_rate_no_trades():
    assert hit_rate([]) == 0.0


# ==========================================
# COMPUTE_METRICS (aggregator)
# ==========================================


def test_compute_metrics_aggregates_all_fields():
    curve = [
        EquitySnapshot(date="2025-06-30", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-06-30", cash=110_000, positions_value=0, total_value=110_000),
    ]
    trades = [_trade(0.10), _trade(-0.05)]

    metrics = compute_metrics(curve, trades)

    assert metrics.total_return == pytest.approx(0.10)
    assert metrics.annualized_return == pytest.approx(0.10, abs=0.001)
    assert metrics.max_drawdown == 0.0
    assert metrics.hit_rate == 0.5
    assert metrics.excess_return_vs_spy is None
    assert metrics.num_trades == 2


def test_compute_metrics_with_spy_baseline():
    portfolio_curve = [
        EquitySnapshot(date="2025-06-30", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-06-30", cash=115_000, positions_value=0, total_value=115_000),
    ]
    spy_curve = [
        EquitySnapshot(date="2025-06-30", cash=100_000, positions_value=0, total_value=100_000),
        EquitySnapshot(date="2026-06-30", cash=108_000, positions_value=0, total_value=108_000),
    ]

    metrics = compute_metrics(portfolio_curve, [], spy_equity_curve=spy_curve)

    # Portfolio +15%, SPY +8% → excess +7%
    assert metrics.excess_return_vs_spy == pytest.approx(0.07, abs=0.001)
