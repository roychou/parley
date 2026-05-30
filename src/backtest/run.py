"""
Backtest entrypoint — wires the production data layer + real LLM supervisor
into the replay loop and runs it.

This is the Day 42 integration seam: it composes the pieces that the unit
tests exercise with stubs.

    cached(run_backtest_supervisor)  ->  MultiAgentStrategy.decision_provider
    get_prices                       ->  run_backtest price_loader
    get_fundamentals_as_of           ->  run_backtest fundamentals_loader
    sp500_as_of / recent_filing_dates -> point-in-time universe + event screen

Two modes:
- Watchlist (default): the static --tickers, multi-agent analyzes them all.
    uv run python -m src.backtest.run
- S&P 500 (the full funnel): point-in-time index membership -> event-driven
  candidate screen (fresh quarterly filers + holdings) -> analysis. Seed caches
  with `python -m src.backtest.backfill` first; expect real LLM cost.
    uv run python -m src.backtest.run --sp500 --dates ...

Specialist signals are cached on disk (signal cache), so re-runs are cheap until
the prompts change (then bump --version).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from functools import partial
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from src.agents.scaffold import ScaffoldConfig
from src.backtest.backtest_supervisor import run_backtest_supervisor
from src.backtest.batch import BatchLLM
from src.backtest.cache import SignalCache
from src.backtest.metrics import StrategyMetrics, compute_metrics
from src.backtest.replay import BacktestConfig, BacktestResult, run_backtest
from src.backtest.strategies import (
    MultiAgentStrategy,
    PERankingStrategy,
    RandomStrategy,
    RSIStrategy,
    SPYHoldStrategy,
)
from src.data.edgar import recent_filing_dates
from src.data.fetch_prices import get_prices
from src.data.fundamentals import get_fundamentals_as_of
from src.data.universe import membership_end, sp500_as_of

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
SIGNAL_CACHE_DIR = Path("data/cache/signals")


# ==========================================
# WIRING
# ==========================================


def build_strategies(
    client: AsyncAnthropic,
    cache_version: str,
    filing_dates_fn=None,
    screen_lookback_days: int = 100,
    include_sentiment: bool = False,
    use_batch: bool = False,
):
    """The comparison set: the multi-agent system under test plus four baselines.

    filing_dates_fn enables the event-driven candidate screen on the multi-agent
    strategy (required at index scale; None analyzes the whole given universe).
    The baselines are deterministic (no LLM) and rank/screen within the universe
    they're handed each period.

    use_batch routes every specialist LLM call through a BatchLLM (Message Batches
    API) instead of firing inline — the fix for tier-1 throttling at index scale.
    The high-concurrency scaffold config lets the sentiment map fan-out coalesce
    into a single batch wave rather than being gated by the live-path semaphore.
    """
    signal_cache = SignalCache(SIGNAL_CACHE_DIR, default_version=cache_version)
    # Bind client + cache -> provider is (ticker, as_of) -> Awaitable[Decision].
    messages_api = BatchLLM(client) if use_batch else None
    scaffold_config = ScaffoldConfig(max_concurrent_chunks=10_000) if use_batch else None
    provider = partial(
        run_backtest_supervisor, client,
        signal_cache=signal_cache, include_sentiment=include_sentiment,
        messages_api=messages_api, scaffold_config=scaffold_config,
    )
    return [
        MultiAgentStrategy(
            decision_provider=provider,
            filing_dates_fn=filing_dates_fn,
            screen_lookback_days=screen_lookback_days,
        ),
        SPYHoldStrategy(),
        RandomStrategy(seed=42),
        RSIStrategy(),
        PERankingStrategy(),
    ]


async def run(
    tickers: list[str],
    dates: list[str],
    cache_version: str,
    use_sp500: bool = False,
    screen_lookback_days: int = 100,
    include_sentiment: bool = False,
    use_batch: bool = False,
) -> BacktestResult:
    client = AsyncAnthropic()
    # sp500 mode: point-in-time S&P 500 eligibility + event-driven candidate screen
    # (mandatory at ~500 names). Watchlist mode: the static --tickers, no screen.
    universe_loader = sp500_as_of if use_sp500 else None
    filing_dates_fn = recent_filing_dates if use_sp500 else None
    config = BacktestConfig(
        universe=[] if use_sp500 else tickers,
        decision_dates=sorted(dates),
        strategies=build_strategies(
            client, cache_version, filing_dates_fn, screen_lookback_days,
            include_sentiment, use_batch,
        ),
        universe_loader=universe_loader,
    )
    # sp500 mode uses the deep ("max") grabbed history; watchlist uses 5y (a 6-month
    # window + indicator lookback reaches past 1y on the earliest dates).
    price_period = "max" if use_sp500 else "5y"
    return await run_backtest(
        config,
        price_loader=_truncating_price_loader(price_period),
        fundamentals_loader=get_fundamentals_as_of,
    )


def _truncating_price_loader(period: str):
    """Price loader that truncates a delisted name's series at its membership end —
    guards against recycled ticker symbols returning a different company's prices
    after the delisting date (see universe.membership_end)."""
    def loader(ticker: str) -> dict:
        prices = get_prices(ticker, period=period)
        end = membership_end(ticker)
        if end is not None:
            prices = {d: bar for d, bar in prices.items() if d <= end}
        return prices
    return loader


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
            periods_per_year=result.config.periods_per_year,
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
    parser.add_argument(
        "--sp500", action="store_true",
        help="Point-in-time S&P 500 universe + event-driven candidate screen "
             "(vs the static --tickers watchlist). Backfill caches first.",
    )
    parser.add_argument(
        "--screen-lookback-days", type=int, default=100,
        help="First-decision filing window for the event screen (sp500 mode).",
    )
    parser.add_argument(
        "--sentiment", action="store_true",
        help="Add the sentiment specialist (reads filing narrative; more LLM calls). "
             "Off = the Release-1 cut line.",
    )
    parser.add_argument(
        "--batch", action="store_true",
        help="Route all specialist LLM calls through the Message Batches API "
             "(coalesced, ~50%% cheaper, no per-minute throttle). Recommended for "
             "the full S&P 500 run, especially with --sentiment.",
    )
    args = parser.parse_args()

    scope = "S&P 500 (PIT) + event screen" if args.sp500 else f"{len(args.tickers)} watchlist"
    sent = " + sentiment" if args.sentiment else ""
    mode = " [batch]" if args.batch else ""
    logger.info(
        f"Backtest: {scope}{sent}{mode} x {len(args.dates)} dates, cache version={args.version}"
    )
    result = asyncio.run(
        run(args.tickers, args.dates, args.version, args.sp500,
            args.screen_lookback_days, args.sentiment, args.batch)
    )
    print_summary(result)


if __name__ == "__main__":
    main()
