"""
Experiment run log — multiple-testing discipline.

Every backtest tweak-and-rerun is, collectively, a search over configurations. If
you try 40 variants and report the best, the "edge" is partly overfit to your own
iteration. The honest defense is to make the search *visible*: append one record per
run (config + headline metrics + an optional note), so the count of variants behind a
result is always knowable.

This is procedural, not statistical — it doesn't correct p-values, it just refuses to
let the experiment count be forgotten. Pair it with the walk-forward out-of-sample
split (validation.py): tune on in-sample across as many runs as you like, but the
honest verdict is the OOS metric on the run you commit to.

The log is JSON Lines at data/experiments/runlog.jsonl (gitignored — a local,
durable, machine-local trail; persistence across runs is the point, not sharing).
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from src.backtest.metrics import compute_metrics
from src.backtest.replay import BacktestResult

logger = logging.getLogger(__name__)

DEFAULT_RUNLOG = Path("data/experiments/runlog.jsonl")


def _git_state() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip())
        return {"commit": commit or None, "dirty": dirty}
    except Exception:  # noqa: BLE001 — git absence shouldn't break a run
        return {"commit": None, "dirty": None}


def _headline_metrics(result: BacktestResult) -> dict[str, Any]:
    """Multi-agent headline numbers (vs SPY), the figures you'd be tempted to chase."""
    ppy = result.config.periods_per_year
    spy = result.outcomes.get("spy_hold")
    spy_curve = spy.portfolio.equity_curve if spy else None
    ma = result.outcomes.get("multi_agent")
    if ma is None:
        return {}
    m = compute_metrics(ma.portfolio.equity_curve, ma.portfolio.closed_trades, spy_curve, ppy)
    excess = m.excess_return_vs_spy
    return {
        "total_return": round(m.total_return, 5),
        "sharpe": round(m.sharpe_ratio, 3),
        "max_drawdown": round(m.max_drawdown, 5),
        "excess_vs_spy": None if excess is None else round(excess, 5),
        "num_trades": m.num_trades,
    }


def log_run(
    result: BacktestResult,
    config_summary: dict[str, Any],
    note: str | None = None,
    path: Path = DEFAULT_RUNLOG,
) -> int:
    """Append one run record; return the run's 1-based sequence number in the log."""
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "git": _git_state(),
        "config": config_summary,
        "metrics": _headline_metrics(result),
        "note": note,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return count_runs(path)


def count_runs(path: Path = DEFAULT_RUNLOG) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())
