"""
Backtest performance metrics — pure functions over (equity_curve, closed_trades).

No state, no Portfolio dependency. Each function returns a scalar.
`compute_metrics()` aggregates them into a StrategyMetrics dataclass.

Sharpe assumes weekly observations (52 periods/year) matching the backtest
design doc's cadence choice. Risk-free rate defaults to 0% (Release 1 limitation;
Release 2 can use T-bill yield).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from src.backtest.portfolio import EquitySnapshot, Trade


@dataclass(frozen=True)
class StrategyMetrics:
    """Summary metrics for one strategy over one backtest run."""
    total_return: float                    # cumulative return over the window
    annualized_return: float               # CAGR
    sharpe_ratio: float                    # risk-adjusted return, weekly-annualized
    max_drawdown: float                    # worst peak-to-trough, negative number
    hit_rate: float                        # fraction of closed trades with positive P&L
    excess_return_vs_spy: float | None     # total_return minus SPY total_return; None if SPY curve not provided
    num_trades: int                        # count of closed trades


def total_return(equity_curve: list[EquitySnapshot]) -> float:
    """Cumulative return: (final - initial) / initial. Returns 0 for empty curve."""
    if not equity_curve:
        return 0.0
    start = equity_curve[0].total_value
    end = equity_curve[-1].total_value
    if start == 0:
        return 0.0
    return (end - start) / start


def annualized_return(equity_curve: list[EquitySnapshot]) -> float:
    """CAGR: (end/start)^(1/years) - 1. Returns 0 if window is too short or empty."""
    if len(equity_curve) < 2:
        return 0.0
    start_value = equity_curve[0].total_value
    end_value = equity_curve[-1].total_value
    if start_value <= 0 or end_value <= 0:
        return 0.0
    years = _years_between(equity_curve[0].date, equity_curve[-1].date)
    if years <= 0:
        return 0.0
    return (end_value / start_value) ** (1 / years) - 1


def sharpe_ratio(
    equity_curve: list[EquitySnapshot],
    periods_per_year: int = 52,
    risk_free_rate: float = 0.0,
) -> float:
    """Sharpe ratio assuming periodic returns and annualizing by sqrt(periods_per_year).

    Default 52 periods/year matches the weekly cadence in the backtest design.
    Returns 0 for insufficient data or zero volatility (no div-by-zero crash).
    """
    if len(equity_curve) < 2:
        return 0.0
    values = [s.total_value for s in equity_curve]
    returns = [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]
    if len(returns) < 2:
        return 0.0

    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return 0.0

    period_risk_free = risk_free_rate / periods_per_year
    excess_return = mean_return - period_risk_free
    return (excess_return / stdev) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: list[EquitySnapshot]) -> float:
    """Worst peak-to-trough decline as a negative fraction (e.g., -0.25 for a 25% drawdown).

    Returns 0.0 for empty curve or curves that only went up.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0].total_value
    worst = 0.0
    for snap in equity_curve:
        if snap.total_value > peak:
            peak = snap.total_value
        if peak > 0:
            dd = (snap.total_value - peak) / peak
            if dd < worst:
                worst = dd
    return worst


def hit_rate(closed_trades: list[Trade]) -> float:
    """Fraction of closed trades with positive realized P&L. Returns 0 if no trades."""
    if not closed_trades:
        return 0.0
    winners = sum(1 for t in closed_trades if t.realized_pnl_pct > 0)
    return winners / len(closed_trades)


def compute_metrics(
    equity_curve: list[EquitySnapshot],
    closed_trades: list[Trade],
    spy_equity_curve: list[EquitySnapshot] | None = None,
    periods_per_year: int = 252,
) -> StrategyMetrics:
    """Aggregate the full metric suite for one strategy.

    Pass an SPY buy-and-hold equity curve to get excess_return_vs_spy populated.
    periods_per_year must match how often the equity curve is sampled: 252 for a
    daily curve (the default, since mark-to-market runs every trading day), 52 if
    the curve is sampled weekly. Mismatching it mis-annualizes Sharpe by
    sqrt(actual / assumed).
    """
    excess = None
    if spy_equity_curve:
        excess = total_return(equity_curve) - total_return(spy_equity_curve)

    return StrategyMetrics(
        total_return=total_return(equity_curve),
        annualized_return=annualized_return(equity_curve),
        sharpe_ratio=sharpe_ratio(equity_curve, periods_per_year=periods_per_year),
        max_drawdown=max_drawdown(equity_curve),
        hit_rate=hit_rate(closed_trades),
        excess_return_vs_spy=excess,
        num_trades=len(closed_trades),
    )


@dataclass(frozen=True)
class AlphaBeta:
    """CAPM-style decomposition of a strategy's returns against the market (SPY)."""
    alpha_annualized: float  # intercept (excess not explained by the market), annualized
    beta: float              # market exposure (1.0 = moves with SPY)
    r_squared: float         # fraction of the strategy's variance the market explains
    n_periods: int


def alpha_beta(
    strategy_curve: list[EquitySnapshot],
    market_curve: list[EquitySnapshot],
    periods_per_year: int = 252,
) -> AlphaBeta:
    """Regress the strategy's periodic returns on the market's (OLS).

    The decisive question for a long-only large-cap strategy: is the return *alpha*
    (skill) or just *beta* (levered market exposure)? High beta + ~zero alpha means
    "you reinvented an index fund with extra steps." Alpha is annualized arithmetically
    (× periods_per_year), consistent with the Sharpe convention here.
    """
    s = {snap.date: snap.total_value for snap in strategy_curve}
    m = {snap.date: snap.total_value for snap in market_curve}
    dates = sorted(s.keys() & m.keys())
    s_ret, m_ret = [], []
    for i in range(1, len(dates)):
        ps, cs = s[dates[i - 1]], s[dates[i]]
        pm, cm = m[dates[i - 1]], m[dates[i]]
        if ps and pm:
            s_ret.append((cs - ps) / ps)
            m_ret.append((cm - pm) / pm)
    n = len(s_ret)
    if n < 2:
        return AlphaBeta(0.0, 0.0, 0.0, n)

    mean_s = sum(s_ret) / n
    mean_m = sum(m_ret) / n
    var_m = sum((r - mean_m) ** 2 for r in m_ret) / (n - 1)
    if var_m == 0:
        return AlphaBeta(0.0, 0.0, 0.0, n)
    cov = sum((s_ret[i] - mean_s) * (m_ret[i] - mean_m) for i in range(n)) / (n - 1)
    beta = cov / var_m
    alpha_period = mean_s - beta * mean_m

    var_s = sum((r - mean_s) ** 2 for r in s_ret) / (n - 1)
    r_squared = (cov * cov) / (var_m * var_s) if var_s > 0 else 0.0

    return AlphaBeta(
        alpha_annualized=alpha_period * periods_per_year,
        beta=beta,
        r_squared=r_squared,
        n_periods=n,
    )


def _years_between(start_date_str: str, end_date_str: str) -> float:
    """Calendar years between two YYYY-MM-DD date strings."""
    start = date.fromisoformat(start_date_str)
    end = date.fromisoformat(end_date_str)
    return (end - start).days / 365.25
