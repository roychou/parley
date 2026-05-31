"""
Walk-forward / out-of-sample reporting.

The danger with an LLM-driven strategy is *overfitting by iteration*: tuning prompts
and thresholds until the backtest looks good. There's no parameter fit inside a single
run, but the human tuning loop between runs is the fit — so the discipline is to
designate an out-of-sample window you commit NOT to tune against, and judge the
strategy there.

This module makes that split explicit. It does NOT re-run anything: it slices a single
BacktestResult's equity curve and trades at a split date into in-sample (≤ split) and
out-of-sample (> split) segments and computes metrics on each. Equity is sliced by
snapshot date; trades by **entry_date** (the decision being evaluated belongs to the
window in which it was made).

It also surfaces the **number of independent bets** (trades) per window — the honest
caveat is that an out-of-sample Sharpe resting on a handful of trades is noise, not
evidence. A single split is the MVP; rolling multi-fold walk-forward is a future
extension (run once per fold, report each).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.backtest.metrics import StrategyMetrics, compute_metrics
from src.backtest.portfolio import EquitySnapshot, Trade
from src.backtest.replay import BacktestResult

# Below this many trades, an out-of-sample metric is not statistically meaningful.
MIN_MEANINGFUL_BETS = 30


@dataclass(frozen=True)
class SegmentMetrics:
    label: str          # "in-sample" | "out-of-sample"
    start: str | None   # first snapshot date in the segment
    end: str | None
    metrics: StrategyMetrics


def split_by_date(
    equity_curve: list[EquitySnapshot],
    closed_trades: list[Trade],
    split_date: str,
) -> tuple[tuple[list[EquitySnapshot], list[Trade]], tuple[list[EquitySnapshot], list[Trade]]]:
    """Partition into ((is_equity, is_trades), (oos_equity, oos_trades)).

    In-sample = snapshots/trades on or before split_date; out-of-sample = strictly
    after. Trades are assigned by entry_date (the decision date)."""
    is_equity = [s for s in equity_curve if s.date <= split_date]
    oos_equity = [s for s in equity_curve if s.date > split_date]
    is_trades = [t for t in closed_trades if t.entry_date <= split_date]
    oos_trades = [t for t in closed_trades if t.entry_date > split_date]
    return (is_equity, is_trades), (oos_equity, oos_trades)


def choose_split_date(decision_dates: list[str], train_frac: float = 0.6) -> str:
    """The decision date at `train_frac` through the sorted schedule — everything
    after it is the out-of-sample window."""
    if not decision_dates:
        raise ValueError("no decision dates to split")
    ordered = sorted(decision_dates)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * train_frac) - 1))
    return ordered[idx]


def _segment(label: str, equity, trades, spy_equity, periods_per_year) -> SegmentMetrics:
    return SegmentMetrics(
        label=label,
        start=equity[0].date if equity else None,
        end=equity[-1].date if equity else None,
        metrics=compute_metrics(equity, trades, spy_equity, periods_per_year),
    )


def walk_forward_metrics(
    result: BacktestResult, split_date: str
) -> dict[str, list[SegmentMetrics]]:
    """Per-strategy [in-sample, out-of-sample] metrics from one already-run backtest.

    SPY's own curve is sliced the same way so excess-vs-SPY stays per-window."""
    ppy = result.config.periods_per_year
    spy = result.outcomes.get("spy_hold")
    spy_is = spy_oos = None
    if spy:
        (spy_is, _), (spy_oos, _) = split_by_date(
            spy.portfolio.equity_curve, spy.portfolio.closed_trades, split_date
        )

    out: dict[str, list[SegmentMetrics]] = {}
    for name, outcome in result.outcomes.items():
        (is_eq, is_tr), (oos_eq, oos_tr) = split_by_date(
            outcome.portfolio.equity_curve, outcome.portfolio.closed_trades, split_date
        )
        is_spy = None if name == "spy_hold" else spy_is
        oos_spy = None if name == "spy_hold" else spy_oos
        out[name] = [
            _segment("in-sample", is_eq, is_tr, is_spy, ppy),
            _segment("out-of-sample", oos_eq, oos_tr, oos_spy, ppy),
        ]
    return out


def _fmt_pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 100:+6.2f}%"


def print_walk_forward(result: BacktestResult, split_date: str) -> None:
    report = walk_forward_metrics(result, split_date)

    print("\n" + "=" * 78)
    print(f"WALK-FORWARD / OUT-OF-SAMPLE  (split at {split_date}; OOS = strictly after)")
    print("=" * 78)
    header = (
        f"{'strategy':<14}{'segment':<15}{'total':>9}{'sharpe':>8}"
        f"{'maxDD':>9}{'hit':>8}{'trades':>8}{'vsSPY':>9}"
    )
    print(header)
    print("-" * 78)
    for name, segments in report.items():
        for seg in segments:
            m = seg.metrics
            print(
                f"{name:<14}{seg.label:<15}{_fmt_pct(m.total_return):>9}"
                f"{m.sharpe_ratio:>8.2f}{_fmt_pct(m.max_drawdown):>9}"
                f"{_fmt_pct(m.hit_rate):>8}{m.num_trades:>8}"
                f"{_fmt_pct(m.excess_return_vs_spy):>9}"
            )
        print("-" * 78)

    # Significance caveat: judge the OOS verdict by the multi-agent's OOS bet count.
    ma = report.get("multi_agent")
    if ma:
        oos_bets = ma[1].metrics.num_trades
        if oos_bets < MIN_MEANINGFUL_BETS:
            print(
                f"⚠️  multi_agent has {oos_bets} out-of-sample trades "
                f"(< {MIN_MEANINGFUL_BETS}) — OOS metrics are indicative, not significant. "
                "Widen the window or cadence before trusting the verdict."
            )
    print("=" * 78 + "\n")
