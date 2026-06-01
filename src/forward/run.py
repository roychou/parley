"""
Forward paper-trading entrypoint — one weekly session, end to end.

Ties the whole forward stack into a single command:
  connect IBKR -> refresh the price cache + news store from one connection ->
  build the decision provider (fundamentals + technicals + sentiment + news, reading
  the IBKR-refreshed caches) -> run_forward_session (screen -> decide -> size via the
  risk layer -> execute against the persistent PaperBook) -> save.

A scheduler (cron/launchd) runs this weekly. Laptop-first: Gateway runs locally during
the ~minutes-long session, then idles. The pure cache-derived helpers
(current_price_from_cache, volatility_from_cache, dividends_since) are unit-tested; the
IBKR connect/refresh and the full orchestration must be **validated live against a
running Gateway** (CI has none) — same honest limit as the adapters.

Prereqs to run: IB Gateway (paper) + US market-data subscription + Benzinga news +
Anthropic credits. Until then this is staged and ready, not runnable.
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
from src.data.dividends import load_dividends
from src.data.edgar import recent_filing_dates
from src.data.fetch_prices import load_latest_cache
from src.data.fundamentals import get_fundamentals_as_of
from src.data.technicals import get_technicals_as_of
from src.data.universe import nasdaq100_as_of
from src.forward.decide import run_forward_decision
from src.forward.ibkr import (
    FORWARD_PRICE_PERIOD,
    connect,
    fetch_news_for,
    news_source_from_store,
    refresh_price_cache,
)
from src.forward.paper import DEFAULT_BOOK_PATH, PaperBook
from src.forward.session import run_forward_session
from src.risk import RiskConfig, annualized_volatility

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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
# ONE FORWARD SESSION (live; validate against Gateway)
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
    """Run one weekly forward paper session and persist the book. `refresh=False` skips
    the IBKR pull (use a warm cache / for testing)."""
    news_store: dict[str, list[dict]] = {}
    if refresh:
        ib = await connect()
        try:
            await refresh_price_cache(ib, tickers, period=FORWARD_PRICE_PERIOD)
            if include_news:
                news_store = await fetch_news_for(ib, tickers, as_of)
        finally:
            ib.disconnect()

    client = AsyncAnthropic()
    base_mc = BatchLLM(client) if use_batch else client.messages
    mc = base_mc
    if max_llm_usd is not None:
        meter = BudgetMeter(max_llm_usd, batch_discount=0.5 if use_batch else 1.0)
        mc = BudgetedMessages(base_mc, meter)

    provider = partial(
        run_forward_decision, mc,
        fundamentals_loader=partial(get_fundamentals_as_of, price_period=FORWARD_PRICE_PERIOD),
        technicals_loader=partial(get_technicals_as_of, price_period=FORWARD_PRICE_PERIOD),
        news_source=news_source_from_store(news_store),
        include_news=include_news,
        signal_cache=SignalCache(SIGNAL_CACHE_DIR),
        summary_cache=FilingSummaryCache(SUMMARY_CACHE_DIR),
        scaffold_config=ScaffoldConfig(max_concurrent_chunks=10_000) if use_batch else None,
    )

    book = PaperBook.load(book_path)
    risk_config = RiskConfig() if use_risk else None
    summary = await run_forward_session(
        book, as_of, tickers,
        decision_provider=provider,
        current_price=current_price_from_cache(FORWARD_PRICE_PERIOD),
        volatility=(volatility_from_cache(FORWARD_PRICE_PERIOD, as_of, RiskConfig().vol_lookback)
                    if use_risk else None),
        filing_dates_fn=recent_filing_dates,
        cost_model=cost_model or CostModel.ibkr_singapore_fixed(),
        dividends=dividends_since(list(book.positions), book.last_run_date, as_of),
        risk_config=risk_config,
    )
    book.save(book_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one weekly forward paper-trading session.")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Universe (default: current Nasdaq-100).")
    parser.add_argument("--as-of", default=date.today().isoformat(),
                        help="Decision date (default today).")
    parser.add_argument("--no-news", action="store_true", help="Disable the news specialist.")
    parser.add_argument("--no-risk", action="store_true",
                        help="Use flat sizing instead of the risk layer.")
    parser.add_argument("--no-batch", action="store_true",
                        help="Inline LLM calls instead of the Batch API.")
    parser.add_argument("--max-llm-usd", type=float, default=None,
                        help="Hard LLM spend cap for the session.")
    parser.add_argument("--no-refresh", action="store_true",
                        help="Skip the IBKR pull (use the warm cache).")
    args = parser.parse_args()

    tickers = args.tickers or nasdaq100_as_of(args.as_of)
    logger.info(f"forward session as_of={args.as_of} | {len(tickers)} tickers | "
                f"news={not args.no_news} risk={not args.no_risk} batch={not args.no_batch}")
    summary = asyncio.run(run_forward_paper_session(
        tickers, args.as_of,
        include_news=not args.no_news, use_risk=not args.no_risk, use_batch=not args.no_batch,
        max_llm_usd=args.max_llm_usd, refresh=not args.no_refresh,
    ))
    print(summary)


if __name__ == "__main__":
    main()
