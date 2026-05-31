"""Alpha/beta decomposition of strategy returns vs the market."""
import pytest

from src.backtest.metrics import alpha_beta
from src.backtest.portfolio import EquitySnapshot


def _curve_from_returns(returns: list[float], start: float = 100.0):
    """Build a daily equity curve from a per-period return series."""
    vals = [start]
    for r in returns:
        vals.append(vals[-1] * (1 + r))
    return [
        EquitySnapshot(date=f"2026-01-{i + 1:02d}", cash=v, positions_value=0.0, total_value=v)
        for i, v in enumerate(vals)
    ]


# varying market returns so var(market) > 0 (constant returns => undefined beta)
_MKT = [0.01, -0.005, 0.02, -0.01, 0.015, -0.008, 0.012]


def test_pure_beta_two_no_alpha():
    market = _curve_from_returns(_MKT)
    strat = _curve_from_returns([2 * r for r in _MKT])  # exactly 2x the market each day
    ab = alpha_beta(strat, market, periods_per_year=252)
    assert ab.beta == pytest.approx(2.0, abs=1e-6)
    assert ab.alpha_annualized == pytest.approx(0.0, abs=1e-6)
    assert ab.r_squared == pytest.approx(1.0, abs=1e-6)


def test_alpha_with_unit_beta():
    market = _curve_from_returns(_MKT)
    strat = _curve_from_returns([r + 0.001 for r in _MKT])  # market + 10bps/day alpha
    ab = alpha_beta(strat, market, periods_per_year=252)
    assert ab.beta == pytest.approx(1.0, abs=1e-6)
    assert ab.alpha_annualized == pytest.approx(0.001 * 252, abs=1e-3)


def test_insufficient_data_is_zero():
    one = [EquitySnapshot(date="2026-01-01", cash=100, positions_value=0, total_value=100)]
    ab = alpha_beta(one, one)
    assert ab.beta == 0.0 and ab.alpha_annualized == 0.0 and ab.n_periods == 0
