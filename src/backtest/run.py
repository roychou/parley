"""
Backtest entrypoint — wires the production data layer + real LLM supervisor
into the replay loop and runs it.

This is the Day 42 integration seam: it composes the pieces that the unit
tests exercise with stubs.

    cached(run_backtest_supervisor)  ->  MultiAgentStrategy.decision_provider
    get_prices                       ->  run_backtest price_loader
    get_fundamentals_as_of           ->  run_backtest fundamentals_loader
    nasdaq100_as_of / recent_filing_dates -> point-in-time universe + event screen

Two modes:
- Watchlist (default): the static --tickers, multi-agent analyzes them all.
    uv run python -m src.backtest.run
- Nasdaq-100 (the full funnel): point-in-time index membership -> event-driven
  candidate screen (fresh quarterly filers + holdings) -> analysis. Expect real LLM
  cost. (Backtest is retained for plumbing/validation; forward paper trading is the
  real evaluation — see notes/productization.md.)
    uv run python -m src.backtest.run --nasdaq100 --dates ...

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
from src.agents.sentiment_specialist import SUMMARY_VERSION, FilingSummaryCache
from src.backtest.backtest_supervisor import run_backtest_supervisor
from src.backtest.batch import BatchLLM
from src.backtest.budget import BudgetedMessages, BudgetMeter
from src.backtest.cache import SignalCache
from src.backtest.costs import CostModel
from src.backtest.metrics import StrategyMetrics, alpha_beta, compute_metrics
from src.backtest.replay import BacktestConfig, BacktestResult, run_backtest
from src.backtest.runlog import log_run
from src.backtest.strategies import (
    MultiAgentStrategy,
    PERankingStrategy,
    RandomStrategy,
    RSIStrategy,
    SPYHoldStrategy,
)
from src.backtest.temporal import DEFAULT_MODEL_CUTOFF, report_and_filter
from src.backtest.validation import choose_split_date, print_walk_forward
from src.data.dividends import load_dividends
from src.data.fetch_prices import get_prices, load_latest_cache
from src.data.filings import recent_filing_dates
from src.data.fundamentals import get_fundamentals_as_of
from src.data.sectors import sector_of
from src.data.technicals import get_technicals_as_of
from src.data.universe import membership_end, nasdaq100_as_of
from src.risk import RiskConfig

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
SUMMARY_CACHE_DIR = Path("data/cache/filing_summaries")


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
    fundamentals_loader=None,
    technicals_loader=None,
    signal_versions: dict[str, str] | None = None,
    summary_version: str | None = None,
    anonymize: bool = False,
    max_llm_usd: float | None = None,
    risk_config: RiskConfig | None = None,
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

    fundamentals_loader / technicals_loader are the supervisor's own point-in-time
    loaders; when None the supervisor uses its defaults (live, 5y). nasdaq100 mode binds
    them to the cached "max" series so per-candidate analysis stays offline too.

    signal_versions overrides the cache version per specialist kind (the rest fall
    back to cache_version); summary_version does the same for the filing-summary
    cache. Bump only the kind whose prompt you changed to recompute just that
    specialist on a re-run — the others stay warm.
    """
    signal_cache = SignalCache(
        SIGNAL_CACHE_DIR, versions=signal_versions, default_version=cache_version
    )
    # Bind client + cache -> provider is (ticker, as_of) -> Awaitable[Decision].
    # A budget cap wraps the message API so all specialist spend (across every ticker
    # and date) accrues to one meter and aborts the run when the cap trips. Without a
    # cap, preserve prior behavior (None -> supervisor uses client.messages directly).
    base_mc = BatchLLM(client) if use_batch else client.messages
    if max_llm_usd is not None:
        meter = BudgetMeter(max_llm_usd, batch_discount=0.5 if use_batch else 1.0)
        messages_api = BudgetedMessages(base_mc, meter)
    else:
        messages_api = BatchLLM(client) if use_batch else None
    scaffold_config = ScaffoldConfig(max_concurrent_chunks=10_000) if use_batch else None
    # Per-filing summary cache (keyed by accession) — reused across decision dates,
    # quarters, and tickers, halving the sentiment scaffold's map-reduce cost.
    summary_cache = (
        FilingSummaryCache(SUMMARY_CACHE_DIR, version=summary_version or SUMMARY_VERSION)
        if include_sentiment else None
    )
    loader_kwargs = {}
    if fundamentals_loader is not None:
        loader_kwargs["fundamentals_loader"] = fundamentals_loader
    if technicals_loader is not None:
        loader_kwargs["technicals_loader"] = technicals_loader
    provider = partial(
        run_backtest_supervisor, client,
        signal_cache=signal_cache, include_sentiment=include_sentiment,
        messages_api=messages_api, scaffold_config=scaffold_config,
        summary_cache=summary_cache, anonymize=anonymize,
        **loader_kwargs,
    )
    return [
        MultiAgentStrategy(
            decision_provider=provider,
            filing_dates_fn=filing_dates_fn,
            screen_lookback_days=screen_lookback_days,
            risk_config=risk_config,
            sector_map_fn=sector_of,
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
    use_nasdaq100: bool = False,
    screen_lookback_days: int = 100,
    include_sentiment: bool = False,
    use_batch: bool = False,
    signal_versions: dict[str, str] | None = None,
    summary_version: str | None = None,
    cost_model: CostModel | None = None,
    anonymize: bool = False,
    max_llm_usd: float | None = None,
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    client = AsyncAnthropic()
    # Anonymized signals must not collide with named ones in the cache — namespace them.
    if anonymize:
        cache_version = f"{cache_version}-anon"
    # nasdaq100 mode: point-in-time Nasdaq-100 eligibility + event-driven candidate screen
    # (mandatory at ~500 names), run offline against the deep ("max") grabbed price
    # cache — coverage-filtered so a name with no cached prices is dropped rather
    # than triggering a live FMP fetch mid-run. Watchlist mode: the static --tickers,
    # no screen, 5y history (live-fetchable on a miss — the universe is tiny).
    if use_nasdaq100:
        universe_loader = _covered_universe(nasdaq100_as_of, "max")
        price_loader = _cached_price_loader("max")
        # Both the replay's baseline pre-load and the supervisor's per-candidate
        # analysis read the as-of close from the cached "max" series — the grab only
        # captured "max" depth, so the default "5y" would miss and try a live fetch.
        fundamentals_loader = partial(get_fundamentals_as_of, price_period="max")
        technicals_loader = partial(get_technicals_as_of, price_period="max")
        filing_dates_fn = recent_filing_dates
    else:
        universe_loader = None
        price_loader = _truncating_price_loader("5y")
        fundamentals_loader = get_fundamentals_as_of
        technicals_loader = None  # supervisor default (live, 5y) is fine for the watchlist
        filing_dates_fn = None
    config = BacktestConfig(
        universe=[] if use_nasdaq100 else tickers,
        decision_dates=sorted(dates),
        strategies=build_strategies(
            client, cache_version, filing_dates_fn, screen_lookback_days,
            include_sentiment, use_batch,
            fundamentals_loader=fundamentals_loader if use_nasdaq100 else None,
            technicals_loader=technicals_loader,
            signal_versions=signal_versions,
            summary_version=summary_version,
            anonymize=anonymize,
            max_llm_usd=max_llm_usd,
            risk_config=risk_config,
        ),
        universe_loader=universe_loader,
        cost_model=cost_model,
    )
    return await run_backtest(
        config,
        price_loader=price_loader,
        fundamentals_loader=fundamentals_loader,
        dividends_loader=load_dividends,  # total-return (cache-only; {} for non-payers)
    )


def _truncate_to_membership(prices: dict, ticker: str) -> dict:
    """Truncate a delisted name's series at its membership end — guards against
    recycled ticker symbols returning a different company's prices after the
    delisting date (see universe.membership_end)."""
    end = membership_end(ticker)
    if end is not None:
        return {d: bar for d, bar in prices.items() if d <= end}
    return prices


def _truncating_price_loader(period: str):
    """Live-capable loader (watchlist mode): cached prices, fetching on a miss."""
    def loader(ticker: str) -> dict:
        return _truncate_to_membership(get_prices(ticker, period=period), ticker)
    return loader


def _cached_price_loader(period: str):
    """Offline loader (nasdaq100 mode): cached history only, never a live FMP fetch.
    A backtest replays grabbed data; an uncached name returns {} and is excluded
    upstream by _covered_universe, so the run can't stall on a live 402."""
    def loader(ticker: str) -> dict:
        prices = load_latest_cache(ticker, period) or {}
        return _truncate_to_membership(prices, ticker)
    return loader


def _covered_universe(universe_loader, period: str):
    """Restrict a point-in-time universe to names with cached price history.
    A name we can't price can't be marked-to-market, traded, or backtested, so it
    is dropped here (and logged) rather than wasting an LLM analysis or hitting a
    live fetch. Keeps the offline backtest honest about its coverage."""
    def loader(date: str) -> list[str]:
        members = universe_loader(date)
        covered = [t for t in members if load_latest_cache(t, period)]
        dropped = len(members) - len(covered)
        if dropped:
            logger.info(
                f"universe {date}: {len(covered)}/{len(members)} price-covered "
                f"({dropped} dropped — no cached prices)"
            )
        return covered
    return loader


# ==========================================
# REPORTING
# ==========================================


def _fmt_pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 100:+6.2f}%"


def print_summary(result: BacktestResult) -> None:
    spy = result.outcomes.get("spy_hold")
    spy_curve = spy.portfolio.equity_curve if spy else None
    # The SPY benchmark is only usable if it actually moved (it needs SPY prices,
    # which the offline nasdaq100 path may not have cached). A flat benchmark makes
    # vs-SPY and alpha/beta meaningless — report that honestly instead of 0.00s.
    benchmark_usable = bool(spy_curve) and len({s.total_value for s in spy_curve}) > 1

    print("\n" + "=" * 72)
    print("BACKTEST VALIDATION SUMMARY")
    costs = result.config.cost_model or CostModel()
    print(f"transaction costs: {costs.describe()}")
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
            spy_equity_curve=(spy_curve if benchmark_usable and name != "spy_hold" else None),
            periods_per_year=result.config.periods_per_year,
        )
        print(
            f"{name:<14}{_fmt_pct(m.total_return):>9}{m.sharpe_ratio:>9.2f}"
            f"{_fmt_pct(m.max_drawdown):>9}{_fmt_pct(m.hit_rate):>9}"
            f"{m.num_trades:>8}{_fmt_pct(m.excess_return_vs_spy):>9}"
        )

    # Alpha vs. beta: is the return skill or just market exposure? (Skips SPY itself,
    # which is beta=1/alpha=0 by definition.) Needs a usable (non-flat) benchmark.
    if not benchmark_usable:
        print("-" * 72)
        print("vs-SPY / alpha-beta: SPY benchmark unavailable (no SPY price data "
              "cached for this run) — those columns are n/a, not zero.")
    if benchmark_usable:
        print("-" * 72)
        print(f"{'alpha/beta vs SPY':<14}{'beta':>9}{'alpha(ann)':>12}{'R^2':>8}{'periods':>9}")
        for name, outcome in result.outcomes.items():
            if name == "spy_hold":
                continue
            ab = alpha_beta(outcome.portfolio.equity_curve, spy_curve,
                            periods_per_year=result.config.periods_per_year)
            print(f"{name:<14}{ab.beta:>9.2f}{_fmt_pct(ab.alpha_annualized):>12}"
                  f"{ab.r_squared:>8.2f}{ab.n_periods:>9}")

    # Decision audit: surface the direction distribution. (HOLDs produce no
    # trades but are still decisions — the thing that surprised us before.)
    ma = result.outcomes.get("multi_agent")
    if ma:
        counts: dict[str, int] = {}
        for d in ma.decisions:
            counts[d.direction] = counts.get(d.direction, 0) + 1
        print("-" * 72)
        print(f"multi_agent decisions logged: {len(ma.decisions)}  ->  {counts or '{}'}")
        div = ma.portfolio.dividends_received
        if div:
            print(f"multi_agent dividends received (total-return): ${div:,.2f}")
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
        "--version", default="v1",
        help="Default signal-cache version (bump when prompts change; re-spends all "
             "specialists). Prefer the per-kind flags below to recompute just one.",
    )
    parser.add_argument(
        "--fundamentals-version", help="Override cache version for fundamentals only."
    )
    parser.add_argument(
        "--technicals-version", help="Override cache version for technicals only."
    )
    parser.add_argument(
        "--sentiment-version", help="Override cache version for sentiment only."
    )
    parser.add_argument(
        "--summary-version",
        help="Override the filing-summary cache version (bump when the map/extraction "
             "prompts change; a sentiment *synthesis*-only tweak should NOT bump this, "
             "so the costly map-reduce stays cached).",
    )
    parser.add_argument(
        "--nasdaq100", action="store_true",
        help="Point-in-time Nasdaq-100 universe + event-driven candidate screen "
             "(vs the static --tickers watchlist). Backfill caches first.",
    )
    parser.add_argument(
        "--screen-lookback-days", type=int, default=100,
        help="First-decision filing window for the event screen (nasdaq100 mode).",
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
             "the full Nasdaq-100 run, especially with --sentiment.",
    )
    # Transaction costs (applied to every fill, all strategies). Defaults model the
    # real intended broker — IBKR Pro Fixed, US stocks, Singapore account: $0.005/sh,
    # $1 min, 1% cap — plus ~5bps/side slippage for liquid large-caps. SWEEP these to
    # test whether the edge survives friction (productization GATE 0); pass
    # --slippage-bps 0 --commission-per-share 0 --min-commission 0 for frictionless.
    parser.add_argument("--slippage-bps", type=float, default=5.0,
                        help="Adverse slippage per fill, basis points (default 5).")
    parser.add_argument("--commission-bps", type=float, default=0.0,
                        help="Commission as bps of notional (default 0; IBKR uses per-share).")
    parser.add_argument("--commission-per-share", type=float, default=0.005,
                        help="Commission per share in dollars (default 0.005, IBKR fixed).")
    parser.add_argument("--min-commission", type=float, default=1.0,
                        help="Per-order commission floor in dollars (default 1.0, IBKR).")
    parser.add_argument("--max-commission-pct", type=float, default=1.0,
                        help="Per-order commission cap as %% of notional (default 1.0, IBKR).")
    parser.add_argument(
        "--oos-split", default=None,
        help="Date (YYYY-MM-DD) splitting in-sample from out-of-sample; prints a "
             "walk-forward report. The OOS window is what you must NOT tune against.",
    )
    parser.add_argument(
        "--oos-frac", type=float, default=None,
        help="Alternative to --oos-split: train fraction (e.g. 0.6) — the split date "
             "is taken from the decision schedule.",
    )
    parser.add_argument("--run-note", default=None,
                        help="Annotate this run in the experiment log (what you changed/why).")
    parser.add_argument("--no-runlog", action="store_true",
                        help="Skip appending to the experiment run log.")
    parser.add_argument(
        "--model-cutoff", default=DEFAULT_MODEL_CUTOFF,
        help=f"Specialist models' training-data cutoff (YYYY-MM-DD). Decision dates after "
             f"it are temporally clean; on/before are contaminated by training memory. "
             f"Default {DEFAULT_MODEL_CUTOFF} = Sonnet 4.6 training cutoff (the decision "
             f"model). Pass '' to disable the check (treats all as engineering validation).",
    )
    parser.add_argument(
        "--clean-only", action="store_true",
        help="Restrict the run to post-cutoff (temporally valid) decision dates. "
             "Requires --model-cutoff.",
    )
    parser.add_argument(
        "--anonymize", action="store_true",
        help="Contamination probe: strip ticker + dates from the numeric specialists "
             "so they reason from figures alone. Run named vs --anonymize and compare "
             "to size the training-memory gap. Sentiment is force-disabled (filing text "
             "self-identifies); signals cache under a separate '-anon' version.",
    )
    parser.add_argument(
        "--max-llm-usd", type=float, default=None,
        help="Hard cap on estimated LLM spend for this run (USD). Aborts when crossed; "
             "computed signals are cached, so raise the cap and re-run to resume. "
             "STRONGLY recommended for any --nasdaq100 run.",
    )
    parser.add_argument(
        "--risk", action="store_true",
        help="Use the risk-management layer for sizing (vol-targeted + per-name cap + "
             "max-gross + drawdown governor) instead of flat base_pct×confidence.",
    )
    args = parser.parse_args()

    if args.nasdaq100 and args.max_llm_usd is None:
        logger.warning(
            "No --max-llm-usd cap set on a nasdaq100 run. A full-index run costs real "
            "money; set a cap to fail safe (the cache makes it resumable)."
        )

    if args.anonymize and args.sentiment:
        logger.warning(
            "--anonymize forces --sentiment off: filing narrative names the company and "
            "cannot be anonymized. Running numeric specialists only."
        )
        args.sentiment = False

    # Temporal-validity gate (productization.md 0.0): report the contamination split
    # and, with --clean-only, restrict to the post-cutoff window before running.
    dates = report_and_filter(sorted(args.dates), args.model_cutoff, args.clean_only)

    cost_model = CostModel(
        commission_bps=args.commission_bps,
        commission_per_share=args.commission_per_share,
        min_commission=args.min_commission,
        max_commission_pct=args.max_commission_pct,
        slippage_bps=args.slippage_bps,
    )

    # Per-kind overrides; kinds left unset fall back to --version in SignalCache.
    signal_versions = {
        kind: ver
        for kind, ver in (
            ("fundamentals", args.fundamentals_version),
            ("technicals", args.technicals_version),
            ("sentiment", args.sentiment_version),
        )
        if ver
    }

    scope = (
        "Nasdaq-100 (PIT) + event screen" if args.nasdaq100
        else f"{len(args.tickers)} watchlist"
    )
    sent = " + sentiment" if args.sentiment else ""
    mode = " [batch]" if args.batch else ""
    overrides = f" overrides={signal_versions}" if signal_versions else ""
    clean_tag = " [clean-only]" if args.clean_only else ""
    logger.info(
        f"Backtest: {scope}{sent}{mode}{clean_tag} x {len(dates)} dates, "
        f"cache version={args.version}{overrides} | costs: {cost_model.describe()}"
    )
    result = asyncio.run(
        run(args.tickers, dates, args.version, args.nasdaq100,
            args.screen_lookback_days, args.sentiment, args.batch,
            signal_versions=signal_versions or None, summary_version=args.summary_version,
            cost_model=cost_model, anonymize=args.anonymize, max_llm_usd=args.max_llm_usd,
            risk_config=RiskConfig() if args.risk else None)
    )
    print_summary(result)

    if args.oos_split or args.oos_frac is not None:
        split = args.oos_split or choose_split_date(dates, args.oos_frac)
        print_walk_forward(result, split)

    # Multiple-testing discipline: record this run (config + headline metrics) so the
    # count of variants behind any "edge" stays visible.
    if not args.no_runlog:
        config_summary = {
            "universe": "nasdaq100" if args.nasdaq100 else "watchlist",
            "n_dates": len(dates), "first": min(dates), "last": max(dates),
            "sentiment": args.sentiment, "batch": args.batch,
            "version": args.version, "signal_versions": signal_versions or None,
            "summary_version": args.summary_version,
            "screen_lookback_days": args.screen_lookback_days,
            "costs": cost_model.describe(),
            "model_cutoff": args.model_cutoff, "clean_only": args.clean_only,
            "anonymize": args.anonymize, "max_llm_usd": args.max_llm_usd,
            "risk": args.risk,
            "oos_split": args.oos_split, "oos_frac": args.oos_frac,
        }
        n = log_run(result, config_summary, note=args.run_note)
        logger.info(f"logged to experiment runlog as run #{n} ({n} variants recorded)")


if __name__ == "__main__":
    main()
