"""
Score-all driver — feed the IC core (ic.py) with REAL model conviction.

The backtest/forward paths only score the event-screened candidate set and only act on a
subset. IC needs a signed conviction for EVERY name in a fixed universe on each clean date
(a name the model declined is information). This module runs the point-in-time supervisor
over the full point-in-time Nasdaq-100 on each given date, emits `ticker -> signed
conviction`, persists it, and computes the cross-sectional IC against forward returns.

Cost discipline (forward-validation-design.md): defaults to the CHEAP signal — fundamentals
+ technicals only, no sentiment/news fan-out. Escalate to sentiment only if the cheap read
shows life. Use --limit / --max-names to bound a first real slice before paying for a full
~100-name x 16-date sweep.

Runs LOCALLY (price + EDGAR caches + Anthropic credits live on the laptop, not the VPS).

    uv run python -m src.backtest.score_all --dates 2026-02-06 --max-names 15 --horizon 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from functools import partial
from pathlib import Path

from anthropic import AsyncAnthropic

from src.agents.scaffold import ScaffoldConfig
from src.agents.sentiment_specialist import SUMMARY_VERSION, FilingSummaryCache
from src.backtest.batch import BatchLLM
from src.backtest.budget import BudgetedMessages, BudgetExceededError, BudgetMeter
from src.backtest.ic import (
    ICResult,
    cross_sectional_ic,
    forward_return,
    ic_summary,
)
from src.backtest.backtest_supervisor import run_backtest_supervisor
from src.backtest.cache import SignalCache
from src.data.fetch_prices import load_latest_cache
from src.data.fundamentals import get_fundamentals_as_of
from src.data.technicals import get_technicals_as_of
from src.data.universe import nasdaq100_as_of
from src.llm import MessageCreator
from src.models import LADDER_MODELS
from src.schemas.signal import Decision
from src.synthesis import SIGNAL_TO_SCORE

logger = logging.getLogger(__name__)

CONVICTION_DIR = Path("data/experiments/conviction")
SUMMARY_CACHE_DIR = Path("data/cache/filing_summaries")
SIGNAL_CACHE_DIR = Path("data/cache/signals")


def signed_conviction(decision: Decision) -> float:
    """Continuous, PRE-THRESHOLD net conviction = mean(confidence x signal) over the
    contributing specialist signals — the exact quantity synthesis computes internally
    (synthesis.py:44) before it collapses to BUY/HOLD/SELL.

    Why not `confidence x sign(direction)`: that thresholded form pins most names to 0.0
    (everything synthesis calls HOLD), destroying the cross-sectional rank spread IC needs
    (the HOLD-collapse seen in the first run). The raw net score keeps each name's distinct
    lean in [-1, 1], so the ranking has something to discriminate on.

    Read-only: recomputed here from `decision.contributing_signals`; synthesis and the live
    bot are NOT touched — this is research-path code the forward clock never imports.
    """
    sigs = decision.contributing_signals
    if not sigs:
        return 0.0
    return sum(s.confidence * SIGNAL_TO_SCORE[s.signal] for s in sigs) / len(sigs)


def price_covered(ticker: str, period: str = "max") -> bool:
    """True if we have cached price history for this name (offline-only — no live fetch)."""
    return load_latest_cache(ticker, period) is not None


async def score_universe(
    client: AsyncAnthropic,
    as_of: str,
    tickers: list[str],
    *,
    include_sentiment: bool = False,
    max_concurrency: int = 6,
    messages_api: MessageCreator | None = None,
    scaffold_config: ScaffoldConfig | None = None,
    summary_cache: FilingSummaryCache | None = None,
    signal_cache: SignalCache | None = None,
    decision_model: str | None = None,
    hybrid_sentiment: bool = False,
) -> dict[str, float]:
    """Run the supervisor over `tickers` as of `as_of`, returning signed conviction per
    name. Names with missing point-in-time data are SKIPPED (absent != neutral) so they
    drop out of the IC correlation rather than masquerading as a 0.0 bet.

    messages_api routes LLM calls (e.g. a budgeted BatchLLM); a BudgetExceededError on any
    name aborts THAT name (returns None) and, since the meter stays tripped, every later
    name skips too — so the spend cap naturally truncates the run and we keep whatever was
    scored before the cap, rather than crashing."""
    fundamentals_loader = partial(get_fundamentals_as_of, price_period="max")
    technicals_loader = partial(get_technicals_as_of, price_period="max")
    sem = asyncio.Semaphore(max_concurrency)
    capped = {"hit": False}

    async def one(ticker: str) -> tuple[str, float | None]:
        async with sem:
            try:
                decision = await run_backtest_supervisor(
                    client, ticker, as_of,
                    fundamentals_loader=fundamentals_loader,
                    technicals_loader=technicals_loader,
                    include_sentiment=include_sentiment,
                    messages_api=messages_api,
                    scaffold_config=scaffold_config,
                    summary_cache=summary_cache,
                    signal_cache=signal_cache,
                    decision_model=decision_model,
                    hybrid_sentiment=hybrid_sentiment,
                )
                return ticker, signed_conviction(decision)
            except BudgetExceededError:  # cap tripped — stop scoring, keep what we have
                capped["hit"] = True
                return ticker, None
            except ValueError as e:  # missing point-in-time data -> skip this name
                logger.info(f"{as_of} {ticker}: skipped ({e})")
                return ticker, None

    pairs = await asyncio.gather(*(one(t) for t in tickers))
    if capped["hit"]:
        logger.warning(f"{as_of}: spend cap reached — universe truncated for this date.")
    return {t: c for t, c in pairs if c is not None}


def persist_convictions(as_of: str, convictions: dict[str, float]) -> Path:
    CONVICTION_DIR.mkdir(parents=True, exist_ok=True)
    out = CONVICTION_DIR / f"{as_of}.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(convictions, f, indent=2, sort_keys=True)
    return out


def ic_for_date(as_of: str, convictions: dict[str, float], horizon_days: int) -> ICResult:
    """Cross-sectional IC for one date: rank conviction vs forward return across names."""
    forward_returns: dict[str, float | None] = {}
    for ticker in convictions:
        prices = load_latest_cache(ticker, "max")
        forward_returns[ticker] = (
            forward_return(prices, as_of, horizon_days) if prices else None
        )
    return cross_sectional_ic(as_of, convictions, forward_returns)


async def run(
    dates: list[str],
    *,
    max_names: int | None,
    horizon_days: int,
    spacing_days: int,
    include_sentiment: bool,
    use_batch: bool = False,
    max_llm_usd: float | None = None,
    decision_model: str | None = None,
    hybrid_sentiment: bool = False,
) -> None:
    client = AsyncAnthropic()

    # LLM seam: optionally route through the Batch API (~50% cheaper, fine for a
    # non-real-time backtest), and/or meter spend against a hard cap. One BudgetMeter
    # spans ALL dates and names so the cap bounds the whole run, not each date. The batch
    # discount is reflected in the meter so the estimate matches the bill.
    base_mc: MessageCreator | None = BatchLLM(client) if use_batch else None
    if max_llm_usd is not None:
        meter = BudgetMeter(max_llm_usd, batch_discount=0.5 if use_batch else 1.0)
        messages_api: MessageCreator | None = BudgetedMessages(
            base_mc if base_mc is not None else client.messages, meter
        )
    else:
        messages_api = base_mc
    # Batch coalesces the sentiment map-reduce into one wave (high chunk concurrency).
    scaffold_config = ScaffoldConfig(max_concurrent_chunks=10_000) if use_batch else None
    # Per-accession filing-summary cache — the dominant saver across ~monthly dates that
    # share a quarterly filing. Only needed when sentiment is on.
    summary_cache = (
        FilingSummaryCache(SUMMARY_CACHE_DIR, version=SUMMARY_VERSION)
        if include_sentiment else None
    )
    # Persistent per-specialist cache: fundamentals keyed by (filing, P/E band), technicals
    # by date, sentiment by filing accession. Computed once, reused across dates AND across
    # re-runs — so iterate-tweak-rerun is cheap (only the changed specialist recomputes).
    # Namespaced by decision model: a different model produces different signals, so a ladder
    # run (e.g. Sonnet 4.0) must NOT read the deployed model's cached signals.
    cache_version = (decision_model or "deployed").replace("/", "_")
    signal_cache = SignalCache(
        SIGNAL_CACHE_DIR,
        versions={k: cache_version for k in ("fundamentals", "technicals", "sentiment")},
    )

    results: list[ICResult] = []
    for as_of in dates:
        universe = [t for t in nasdaq100_as_of(as_of) if price_covered(t)]
        if max_names is not None:
            universe = universe[:max_names]
        logger.info(
            f"{as_of}: scoring {len(universe)} names "
            f"(model={decision_model or 'deployed'}, sentiment={include_sentiment}"
            f"{'/hybrid' if hybrid_sentiment else ''}, batch={use_batch}, cap={max_llm_usd})"
        )
        convictions = await score_universe(
            client, as_of, universe,
            include_sentiment=include_sentiment,
            messages_api=messages_api,
            scaffold_config=scaffold_config,
            summary_cache=summary_cache,
            signal_cache=signal_cache,
            decision_model=decision_model,
            hybrid_sentiment=hybrid_sentiment,
        )
        path = persist_convictions(as_of, convictions)
        res = ic_for_date(as_of, convictions, horizon_days)
        results.append(res)
        ic_str = f"{res.ic:+.3f}" if res.ic is not None else "n/a"
        print(f"{as_of}: scored {len(convictions)} names -> IC={ic_str} (n={res.n})  [{path}]")

    summary = ic_summary(results, horizon_days, spacing_days)
    print("\n=== IC SUMMARY ===")
    for k, v in asdict(summary).items():
        print(f"  {k}: {v}")
    if summary.overlap_warning:
        print("  ! overlap_warning: horizon > spacing -> t_stat is optimistic "
              "(use non-overlapping dates or Newey-West).")
    if summary.n_dates < 2:
        print("  ! single date: this is a PIPELINE check on real data, not an edge read. "
              "Mean IC over <2 dates has no t-stat.")


def main() -> None:
    p = argparse.ArgumentParser(description="Score the full universe and compute IC.")
    p.add_argument("--dates", nargs="+", required=True, help="Decision dates (YYYY-MM-DD).")
    p.add_argument("--max-names", type=int, default=None,
                   help="Cap names per date (cost control for a first real slice).")
    p.add_argument("--horizon", type=int, default=20,
                   help="Forward-return horizon in trading days (default 20 ~= 4 weeks).")
    p.add_argument("--spacing", type=int, default=5,
                   help="Trading days between decision dates (weekly=5); for overlap flag.")
    p.add_argument("--sentiment", action="store_true",
                   help="Include the sentiment specialist (more cost). Default: cheap signal.")
    p.add_argument("--hybrid-sentiment", action="store_true",
                   help="Use the carry-forward hybrid sentiment (diff + carried tone memo, "
                        "~1 call/filing) instead of the full re-summarize specialist. "
                        "Requires --sentiment and dates processed oldest-first.")
    p.add_argument("--batch", action="store_true",
                   help="Route LLM calls through the Batch API (~50%% cheaper; backtest only).")
    p.add_argument("--max-llm-usd", type=float, default=None,
                   help="Hard spend cap (USD). Run truncates and keeps scored names when hit.")
    p.add_argument("--model", default=None,
                   help="Decision-model override for the cutoff ladder (root+leaf), e.g. "
                        "claude-sonnet-4-20250514. Must be in models.LADDER_MODELS. "
                        "Default: deployed ROOT. The caller must supply --dates within the "
                        "model's clean (post-training-cutoff) window.")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.model is not None and args.model not in LADDER_MODELS:
        raise SystemExit(
            f"--model {args.model} not in models.LADDER_MODELS ({list(LADDER_MODELS)}). "
            "Add it with a verified training cutoff before use (temporal.py contamination guard)."
        )
    if args.model is not None:
        cutoff = LADDER_MODELS[args.model].training_cutoff
        bad = [d for d in args.dates if d <= cutoff]
        if bad:
            raise SystemExit(
                f"--model {args.model} has training cutoff {cutoff}; dates {bad} are on/before it "
                "(contaminated). Supply only post-cutoff dates for a clean ladder run."
            )
    asyncio.run(run(
        args.dates,
        max_names=args.max_names,
        horizon_days=args.horizon,
        spacing_days=args.spacing,
        include_sentiment=args.sentiment,
        use_batch=args.batch,
        max_llm_usd=args.max_llm_usd,
        decision_model=args.model,
        hybrid_sentiment=args.hybrid_sentiment,
    ))


if __name__ == "__main__":
    main()
