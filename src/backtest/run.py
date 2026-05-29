"""
Backtest entrypoint — wires the production data layer + real LLM supervisor
into the replay loop and runs it.

This is the Day 42 integration seam: it composes the pieces that the unit
tests exercise with stubs.

    cached(run_backtest_supervisor)  ->  MultiAgentStrategy.decision_provider
    get_prices                       ->  run_backtest price_loader
    get_fundamentals_as_of           ->  run_backtest fundamentals_loader

Run the default small-window validation (1 month, 3 tickers):

    uv run python -m src.backtest.run

Decisions are cached on disk by (ticker, date) under the prompt `version`, so
re-runs are free until the specialist prompts change (then bump --version).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from functools import partial
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from src.backtest.backtest_supervisor import run_backtest_supervisor
from src.backtest.cache import DecisionCache, make_cached_provider
from src.backtest.metrics import StrategyMetrics, compute_metrics
from src.backtest.replay import BacktestConfig, BacktestResult, run_backtest
from src.backtest.strategies import MultiAgentStrategy, SPYHoldStrategy
from src.data.fetch_prices import get_prices
from src.data.fundamentals import get_fundamentals_as_of

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Small-window validation defaults: 1 month, 3 liquid universe members,
# weekly Fridays. Dates stop before "today" so FMP has posted the EOD close.
DEFAULT_TICKERS = ["NVDA", "MSFT", "GOOGL"]
DEFAULT_DATES = ["2026-04-24", "2026-05-01", "2026-05-08", "2026-05-15", "2026-05-22"]
DECISION_CACHE_DIR = Path("data/cache/decisions")


# ==========================================
# WIRING
# ==========================================


def build_strategies(client: AsyncAnthropic, cache_version: str):
    """Compose the real cached supervisor into the multi-agent strategy.

    SPYHold rides along as the market benchmark — it also exercises the
    deterministic (non-LLM) strategy path end-to-end.
    """
    cache = DecisionCache(DECISION_CACHE_DIR, version=cache_version)
    # partial binds the client -> (ticker, as_of) -> Awaitable[Decision]
    supervisor_fn = partial(run_backtest_supervisor, client)
    provider = make_cached_provider(supervisor_fn, cache)
    return [MultiAgentStrategy(decision_provider=provider), SPYHoldStrategy()]


async def run(tickers: list[str], dates: list[str], cache_version: str) -> BacktestResult:
    client = AsyncAnthropic()
    config = BacktestConfig(
        universe=tickers,
        decision_dates=sorted(dates),
        strategies=build_strategies(client, cache_version),
    )
    return await run_backtest(
        config,
        price_loader=get_prices,
        fundamentals_loader=get_fundamentals_as_of,
    )


# ==========================================
# REPORTING
# ==========================================


def _fmt_pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 100:+6.2f}%"


def print_summary(result: BacktestResult) -> None:
    spy = result.outcomes.get("spy_hold")
    spy_curve = spy.portfolio.equity_curve if spy else None

    print("\n" + "=" * 72)
    print("BACKTEST VALIDATION SUMMARY")
    print("=" * 72)
    header = (
        f"{'strategy':<14}{'total':>9}{'sharpe':>9}{'maxDD':>9}"
        f"{'hit':>9}{'trades':>8}{'vs SPY':>9}"
    )
    print(header)
    print("-" * 72)
    for name, outcome in result.outcomes.items():
        m: StrategyMetrics = compute_metrics(
            outcome.portfolio.equity_curve,
            outcome.portfolio.closed_trades,
            spy_equity_curve=None if name == "spy_hold" else spy_curve,
        )
        print(
            f"{name:<14}{_fmt_pct(m.total_return):>9}{m.sharpe_ratio:>9.2f}"
            f"{_fmt_pct(m.max_drawdown):>9}{_fmt_pct(m.hit_rate):>9}"
            f"{m.num_trades:>8}{_fmt_pct(m.excess_return_vs_spy):>9}"
        )

    # Decision audit: surface the direction distribution. (HOLDs produce no
    # trades but are still decisions — the thing that surprised us before.)
    ma = result.outcomes.get("multi_agent")
    if ma:
        counts: dict[str, int] = {}
        for d in ma.decisions:
            counts[d.direction] = counts.get(d.direction, 0) + 1
        print("-" * 72)
        print(f"multi_agent decisions logged: {len(ma.decisions)}  ->  {counts or '{}'}")
    print("=" * 72 + "\n")


# ==========================================
# CLI
# ==========================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the backtest replay loop.")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS, help="Universe tickers.")
    parser.add_argument(
        "--dates", nargs="+", default=DEFAULT_DATES, help="Decision dates (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--version", default="v1", help="Decision-cache version (bump when prompts change)."
    )
    args = parser.parse_args()

    logger.info(
        f"Backtest: {len(args.tickers)} tickers x {len(args.dates)} dates, "
        f"cache version={args.version}"
    )
    result = asyncio.run(run(args.tickers, args.dates, args.version))
    print_summary(result)


if __name__ == "__main__":
    main()
