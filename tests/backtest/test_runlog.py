"""Experiment run log (multiple-testing discipline)."""
import json

import pytest

from src.backtest.replay import BacktestConfig, run_backtest
from src.backtest.runlog import count_runs, log_run
from src.backtest.strategies import SPYHoldStrategy

_DATES = ["2026-01-09", "2026-01-16", "2026-01-23"]


async def _tiny_result():
    history = {
        "SPY": {d: {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
                for d, c in zip(_DATES, [400.0, 404.0, 408.0])}
    }
    config = BacktestConfig(
        universe=["SPY"], decision_dates=_DATES,
        strategies=[SPYHoldStrategy()], stop_loss_pct=None,
    )
    return await run_backtest(config, lambda t: history[t], lambda t, d: None)


@pytest.mark.asyncio
async def test_log_run_appends_and_counts(tmp_path):
    result = await _tiny_result()
    path = tmp_path / "runlog.jsonl"

    n1 = log_run(result, {"universe": "watchlist", "sentiment": False}, note="first", path=path)
    n2 = log_run(
        result, {"universe": "watchlist", "sentiment": True}, note="tried sentiment", path=path
    )

    assert (n1, n2) == (1, 2)
    assert count_runs(path) == 2

    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    assert rec["note"] == "tried sentiment"
    assert rec["config"]["sentiment"] is True
    assert "timestamp" in rec and "git" in rec and "metrics" in rec


def test_count_runs_missing_file(tmp_path):
    assert count_runs(tmp_path / "nope.jsonl") == 0
