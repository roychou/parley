"""
Forward paper-trading entrypoint — one weekly session, end to end.

Ties the whole forward stack into a single command:
  refresh prices (+news) -> build the decision provider (fundamentals + technicals +
  sentiment + news) -> run_forward_session (screen -> decide -> size via the risk
  layer -> execute against the persistent PaperBook) -> save.

Prices + Benzinga news come from a running IB Gateway/TWS (the only data source —
fundamentals come from EDGAR, free). Needs the Gateway up + a market-data
subscription; the connect/refresh must be validated live (CI has none).

A scheduler (cron/launchd) runs this weekly. The cache-derived helpers
(current_price_from_cache, volatility_from_cache, dividends_since) are unit-tested.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, timedelta
from functools import partial
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from src.agents.scaffold import ScaffoldConfig
from src.agents.sentiment_specialist import FilingSummaryCache
from src.backtest.batch import BatchLLM
from src.backtest.budget import BudgetedMessages, BudgetMeter
from src.backtest.cache import SignalCache
from src.backtest.costs import CostModel
from src.backtest.screen import select_candidates
from src.data.dividends import load_dividends
from src.data.fetch_prices import load_latest_cache
from src.data.filings import recent_filing_dates
from src.data.fundamentals import get_fundamentals_as_of
from src.data.technicals import get_technicals_as_of
from src.data.universe import nasdaq100_as_of
from src.forward.decide import run_forward_decision
from src.forward.ibkr import (
    FORWARD_PRICE_PERIOD,
    assert_paper_ready,
    connect,
    fetch_news_for,
    news_source_from_store,
    refresh_price_cache,
)
from src.forward.notify import (
    heartbeat_stale,
    notify,
    read_heartbeat,
    write_heartbeat,
)
from src.forward.paper import DEFAULT_BOOK_PATH, PaperBook
from src.forward.report import session_digest, write_digest
from src.forward.session import run_forward_session
from src.risk import RiskConfig, annualized_volatility

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCREEN_LOOKBACK_DAYS = 7  # trailing window for the filing-event screen
SIGNAL_CACHE_DIR = Path("data/cache/signals")
SUMMARY_CACHE_DIR = Path("data/cache/filing_summaries")


# ==========================================
# CACHE-DERIVED HELPERS (unit-tested)
# ==========================================


def current_price_from_cache(period: str = FORWARD_PRICE_PERIOD):
    """Latest cached close for a ticker (the IBKR refresh writes under `period`)."""
    def price(ticker: str) -> float | None:
        prices = load_latest_cache(ticker, period) or {}
        if not prices:
            return None
        return float(prices[max(prices)]["close"])
    return price


def volatility_from_cache(period: str, as_of: str, lookback: int):
    """Annualized vol from the cached series as of the date (for risk sizing)."""
    def vol(ticker: str) -> float | None:
        return annualized_volatility(load_latest_cache(ticker, period) or {}, as_of, lookback)
    return vol


def dividends_since(tickers: list[str], last_run_date: str | None, as_of: str) -> dict[str, float]:
    """Per-share dividends with an ex-date in (last_run_date, as_of] — i.e. everything
    since the previous session — so a weekly cadence still credits inter-session
    dividends. With no prior run, only the as_of date's dividends are credited."""
    lo = last_run_date or _prev_day(as_of)
    out: dict[str, float] = {}
    for t in tickers:
        total = sum(div for ex, div in load_dividends(t).items() if lo < ex <= as_of)
        if total:
            out[t] = total
    return out


def _prev_day(d: str) -> str:
    y, m, day = (int(x) for x in d.split("-"))
    return (date(y, m, day) - timedelta(days=1)).isoformat()


# ==========================================
# ONE FORWARD SESSION
# ==========================================


async def run_forward_paper_session(
    tickers: list[str],
    as_of: str,
    *,
    include_news: bool = True,
    use_batch: bool = True,
    use_risk: bool = True,
    max_llm_usd: float | None = None,
    refresh: bool = True,
    cost_model: CostModel | None = None,
    book_path: Path = DEFAULT_BOOK_PATH,
) -> dict:
    """Run one weekly forward paper session and persist the book.

    Prices + news come from a running IB Gateway/TWS. `refresh=False` skips the pull
    and runs against the warm cache.
    """
    period = FORWARD_PRICE_PERIOD
    book = PaperBook.load(book_path)

    # Screen FIRST (filing dates, not prices), then refresh only the names we'll
    # actually analyze — candidates + holdings, a handful — instead of the whole
    # universe. Keeps the per-session data pull small and under IBKR's historical-
    # request pacing limit.
    held = list(book.positions)
    window_start = (date.fromisoformat(as_of) - timedelta(days=SCREEN_LOOKBACK_DAYS)).isoformat()
    candidates = select_candidates(tickers, held, window_start, as_of, recent_filing_dates)
    logger.info(f"screen: {len(candidates)} candidates "
                f"({len(tickers)} universe + {len(held)} held)")

    news_store: dict[str, list[dict]] = {}
    if refresh:
        ib = await connect()
        try:
            assert_paper_ready(ib)  # preflight: fail fast before any LLM spend
            await refresh_price_cache(ib, candidates, period=period)
            if include_news:
                news_store = await fetch_news_for(ib, candidates, as_of)
        finally:
            ib.disconnect()

    news_source = news_source_from_store(news_store if include_news else {})

    client = AsyncAnthropic()
    base_mc = BatchLLM(client) if use_batch else client.messages
    mc = base_mc
    if max_llm_usd is not None:
        meter = BudgetMeter(max_llm_usd, batch_discount=0.5 if use_batch else 1.0)
        mc = BudgetedMessages(base_mc, meter)

    provider = partial(
        run_forward_decision, mc,
        fundamentals_loader=partial(get_fundamentals_as_of, price_period=period),
        technicals_loader=partial(get_technicals_as_of, price_period=period),
        news_source=news_source,
        include_news=include_news,
        signal_cache=SignalCache(SIGNAL_CACHE_DIR),
        summary_cache=FilingSummaryCache(SUMMARY_CACHE_DIR),
        scaffold_config=ScaffoldConfig(max_concurrent_chunks=10_000) if use_batch else None,
    )

    risk_config = RiskConfig() if use_risk else None
    summary = await run_forward_session(
        book, as_of, tickers,
        candidates=candidates,
        decision_provider=provider,
        current_price=current_price_from_cache(period),
        volatility=(volatility_from_cache(period, as_of, RiskConfig().vol_lookback)
                    if use_risk else None),
        filing_dates_fn=recent_filing_dates,
        cost_model=cost_model or CostModel.ibkr_singapore_fixed(),
        dividends=dividends_since(list(book.positions), book.last_run_date, as_of),
        risk_config=risk_config,
    )
    book.save(book_path)
    digest = session_digest(book, summary)
    logger.info("session digest -> %s\n%s", write_digest(as_of, digest), digest)
    return summary


def _summary_line(summary: dict) -> str:
    """A compact one-line status for the alert email / heartbeat note."""
    try:
        return (f"as_of {summary['as_of']} | decided {summary['decided']}/"
                f"{summary['candidates']} | open {summary['open_positions']} | "
                f"equity ${summary['equity']:,.0f} | {summary['directions']}")
    except Exception:  # noqa: BLE001 — never let formatting break the wrap-up
        return str(summary)[:300]


def _run_healthcheck(max_age_hours: float) -> None:
    """Standalone watchdog (for a separate, more frequent cron): emails if the clock
    has gone quiet — no heartbeat, last run errored, or older than max_age_hours.
    Exits non-zero when stale so the scheduler also records it."""
    hb = read_heartbeat()
    if heartbeat_stale(hb, max_age_hours):
        body = f"forward clock looks stale (>{max_age_hours}h or errored). Last: {hb}"
        logger.warning(body)
        notify("⚠️ parley forward clock is quiet", body)
        raise SystemExit(1)
    logger.info(f"heartbeat OK: {hb}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one weekly forward paper-trading session.")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Universe (default: current Nasdaq-100).")
    parser.add_argument("--as-of", default=date.today().isoformat(),
                        help="Decision date (default today).")
    parser.add_argument("--book", default=None,
                        help="PaperBook path (default data/forward/paper_book.json).")
    parser.add_argument("--no-news", action="store_true", help="Disable the news specialist.")
    parser.add_argument("--no-risk", action="store_true",
                        help="Use flat sizing instead of the risk layer.")
    parser.add_argument("--no-batch", action="store_true",
                        help="Inline LLM calls instead of the Batch API.")
    parser.add_argument("--max-llm-usd", type=float, default=None,
                        help="Hard LLM spend cap for the session.")
    parser.add_argument("--no-refresh", action="store_true",
                        help="Skip the IBKR pull (use the warm cache).")
    parser.add_argument("--healthcheck", action="store_true",
                        help="Watchdog mode: email if the last run is stale/errored, then exit.")
    parser.add_argument("--heartbeat-max-hours", type=float, default=24 * 8,
                        help="Healthcheck staleness threshold in hours (default 8 days).")
    args = parser.parse_args()

    if args.healthcheck:
        _run_healthcheck(args.heartbeat_max_hours)
        return

    tickers = args.tickers or nasdaq100_as_of(args.as_of)
    logger.info(f"forward session as_of={args.as_of} | {len(tickers)} "
                f"tickers | news={not args.no_news} risk={not args.no_risk} "
                f"batch={not args.no_batch}")
    try:
        summary = asyncio.run(run_forward_paper_session(
            tickers, args.as_of,
            include_news=not args.no_news, use_risk=not args.no_risk, use_batch=not args.no_batch,
            max_llm_usd=args.max_llm_usd, refresh=not args.no_refresh,
            book_path=Path(args.book) if args.book else DEFAULT_BOOK_PATH,
        ))
    except Exception as e:  # noqa: BLE001 — record + alert, then re-raise for a non-zero exit
        body = f"{type(e).__name__}: {e}"
        logger.exception("forward session failed")
        write_heartbeat("error", args.as_of, body)
        notify(f"❌ parley forward FAILED ({args.as_of})", body)
        raise

    note = _summary_line(summary)
    write_heartbeat("ok", args.as_of, note)
    notify(f"✅ parley forward OK ({args.as_of})", note)
    print(summary)


if __name__ == "__main__":
    main()
