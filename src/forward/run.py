"""
Forward paper-trading entrypoint — one weekly session, end to end.

Ties the whole forward stack into a single command:
  refresh prices (+news) -> build the decision provider (fundamentals + technicals +
  sentiment + news) -> run_forward_session (screen -> decide -> size via the risk
  layer -> execute against the persistent PaperBook) -> save.

Two price/news sources (--source):
- "fmp": daily bars + news from FMP, no broker. The paper book is simulated, so IBKR
  is NOT needed to run the forward clock — only later for real order execution. This is
  the runnable path today.
- "ibkr": prices + Benzinga news from a running IB Gateway/TWS. Needs the Gateway up +
  a market-data subscription; the connect/refresh must be validated live (CI has none).

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
from src.data.fetch_prices import get_prices, load_latest_cache
from src.data.filings import recent_filing_dates
from src.data.fmp_client import get_stock_news
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


def refresh_prices_fmp(tickers: list[str], period: str = "5y") -> None:
    """Forward price refresh with no broker: fetch + cache daily bars from FMP — the
    same source the backtest uses. Lets the weekly clock run without IB Gateway/TWS;
    IBKR is only needed later for real order execution (the paper book is simulated)."""
    ok = 0
    for t in tickers:
        try:
            get_prices(t, period)  # fetches from FMP and writes the period cache
            ok += 1
        except Exception as e:  # one bad symbol must not abort the weekly run
            logger.warning(f"FMP price refresh failed for {t}: {e}")
    logger.info(f"FMP price refresh: {ok}/{len(tickers)} tickers cached (period={period})")


def fmp_news_source():
    """A lazy NewsSource backed by FMP: fetches a ticker's recent news on demand (only
    for screened candidates), point-in-time filtered to published <= as_of."""
    from datetime import date as _date

    def source(ticker: str, as_of: str, lookback_days: int) -> list[dict]:
        start = (_date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
        out: list[dict] = []
        for r in get_stock_news(ticker, start, as_of, limit=50):
            pub = (r.get("publishedDate") or "")[:10]
            if not pub or pub > as_of or pub < start:  # point-in-time + window
                continue
            out.append({
                "title": r.get("title", ""),
                "summary": r.get("text", ""),
                "published": pub,
                "source": r.get("site") or r.get("publisher", ""),
                "url": r.get("url", ""),
            })
        return out

    return source


# ==========================================
# ONE FORWARD SESSION
# ==========================================


async def run_forward_paper_session(
    tickers: list[str],
    as_of: str,
    *,
    price_source: str = "ibkr",
    include_news: bool = True,
    use_batch: bool = True,
    use_risk: bool = True,
    max_llm_usd: float | None = None,
    refresh: bool = True,
    cost_model: CostModel | None = None,
    book_path: Path = DEFAULT_BOOK_PATH,
) -> dict:
    """Run one weekly forward paper session and persist the book.

    price_source: "ibkr" pulls prices + news from a running IB Gateway/TWS; "fmp"
    pulls daily bars from FMP with no broker (the book is simulated, so IBKR is only
    needed later for real order execution). `refresh=False` skips the pull (warm cache).
    """
    period = FORWARD_PRICE_PERIOD if price_source == "ibkr" else "5y"
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
        if price_source == "ibkr":
            ib = await connect()
            try:
                await refresh_price_cache(ib, candidates, period=period)
                if include_news:
                    news_store = await fetch_news_for(ib, candidates, as_of)
            finally:
                ib.disconnect()
        else:
            refresh_prices_fmp(candidates, period)

    # News source: FMP fetches lazily per candidate; IBKR uses the pre-fetched store.
    if not include_news:
        news_source = news_source_from_store({})
    elif price_source == "fmp":
        news_source = fmp_news_source()
    else:
        news_source = news_source_from_store(news_store)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one weekly forward paper-trading session.")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Universe (default: current Nasdaq-100).")
    parser.add_argument("--as-of", default=date.today().isoformat(),
                        help="Decision date (default today).")
    parser.add_argument("--source", choices=["ibkr", "fmp"], default="ibkr",
                        help="Price/news source: ibkr (Gateway/TWS) or fmp (no broker).")
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
    logger.info(f"forward session as_of={args.as_of} | source={args.source} | {len(tickers)} "
                f"tickers | news={not args.no_news} risk={not args.no_risk} "
                f"batch={not args.no_batch}")
    summary = asyncio.run(run_forward_paper_session(
        tickers, args.as_of,
        price_source=args.source,
        include_news=not args.no_news, use_risk=not args.no_risk, use_batch=not args.no_batch,
        max_llm_usd=args.max_llm_usd, refresh=not args.no_refresh,
    ))
    print(summary)


if __name__ == "__main__":
    main()
