"""print_summary honesty: a flat/unavailable SPY benchmark is flagged, not shown as 0."""
from src.backtest.portfolio import EquitySnapshot, Portfolio
from src.backtest.replay import BacktestConfig, BacktestResult, StrategyOutcome
from src.backtest.run import print_summary


def _outcome(name, totals):
    p = Portfolio()
    p.equity_curve = [
        EquitySnapshot(date=f"2026-01-{i + 1:02d}", cash=t, positions_value=0.0, total_value=t)
        for i, t in enumerate(totals)
    ]
    return StrategyOutcome(name=name, portfolio=p, decisions=[])


def _result(spy_totals):
    config = BacktestConfig(universe=["SPY"], decision_dates=["2026-01-01"], strategies=[])
    return BacktestResult(config=config, outcomes={
        "spy_hold": _outcome("spy_hold", spy_totals),
        "multi_agent": _outcome("multi_agent", [100_000, 101_000, 102_000]),
    })


def test_flat_benchmark_is_flagged_not_zeroed(capsys):
    print_summary(_result([100_000, 100_000, 100_000]))  # SPY flat -> unusable
    out = capsys.readouterr().out
    assert "benchmark unavailable" in out
    assert "alpha/beta vs SPY" not in out          # block skipped, no misleading 0.00 betas


def test_usable_benchmark_shows_alpha_beta(capsys):
    print_summary(_result([100_000, 100_500, 101_200]))  # SPY moves -> usable
    out = capsys.readouterr().out
    assert "alpha/beta vs SPY" in out
    assert "benchmark unavailable" not in out
