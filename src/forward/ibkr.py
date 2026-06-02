"""
IBKR data adapters for forward paper trading — prices and news.

Design (the decoupling we settled on): IBKR's API is async and a single connection
shouldn't be driven by the decision loop, so these adapters are **refresh/ingest**
steps. An async refresh pulls daily bars + recent news from one IB Gateway connection
into our existing stores; the sync injected interfaces (price cache → technicals;
`NewsSource` → news specialist) then read those stores during the decision loop. No
streaming, no 24/7 process — a weekly session calls the refresh, then decides.

The pure converters (`_bars_to_price_dict`, `_news_to_articles`,
`news_source_from_store`) are unit-tested. The IBKR IO functions are thin wrappers over
`ib_async` and must be **validated live against a running IB Gateway** — they can't be
exercised in CI. Connection is host/port-configurable (env) so laptop → cloud VM is a
config change, per the deployment discussion.

Requires: a running IB Gateway/TWS + US market-data subscription (prices) and a news
subscription (Benzinga is free with an IBKR account). Execution is a separate, later
adapter.
"""
from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterable

from ib_async import IB, Stock

from src.agents.news_specialist import NewsSource
from src.data.fetch_prices import save_prices_to_cache

logger = logging.getLogger(__name__)

# Defaults: IB Gateway paper = 4002 (TWS paper = 7497). Overridable via env so the
# same code runs against a local Gateway or one on a cloud VM.
DEFAULT_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("IBKR_PORT", "4002"))
DEFAULT_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "17"))

# Price cache period label the forward path reads (get_prices/get_technicals_as_of).
FORWARD_PRICE_PERIOD = "ibkr"


async def connect(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, client_id: int = DEFAULT_CLIENT_ID
) -> IB:
    """Connect to a running IB Gateway/TWS. Caller disconnects (ib.disconnect())."""
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id)
    logger.info(f"connected to IBKR at {host}:{port} (clientId={client_id})")
    return ib


# ==========================================
# PURE CONVERTERS (unit-tested)
# ==========================================


def _bars_to_price_dict(bars: Iterable) -> dict[str, dict]:
    """ib_async BarData list -> our {date: {open,high,low,close,volume}} format."""
    out: dict[str, dict] = {}
    for b in bars:
        d = b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date)[:10]
        out[d] = {
            "open": round(float(b.open), 2),
            "high": round(float(b.high), 2),
            "low": round(float(b.low), 2),
            "close": round(float(b.close), 2),
            "volume": int(b.volume) if b.volume and b.volume > 0 else 0,
        }
    return out


def _news_to_articles(items: Iterable, bodies: dict[str, str]) -> list[dict]:
    """ib_async HistoricalNews items (+ fetched bodies keyed by articleId) -> our
    article dicts {title, summary, published, source, url}."""
    out: list[dict] = []
    for it in items:
        published = str(getattr(it, "time", ""))[:10]  # 'YYYY-MM-DD ...' -> date
        out.append({
            "title": getattr(it, "headline", "") or "",
            "summary": bodies.get(getattr(it, "articleId", ""), ""),
            "published": published,
            "source": getattr(it, "providerCode", "") or "",
            "url": "",
        })
    return out


def news_source_from_store(store: dict[str, list[dict]]) -> NewsSource:
    """Wrap a pre-fetched {ticker: [articles]} store as a sync NewsSource. The async
    IBKR fetch (fetch_news_for) populates the store before the decision loop runs."""
    def source(ticker: str, as_of: str, lookback_days: int) -> list[dict]:
        return store.get(ticker, [])
    return source


# ==========================================
# IBKR IO (thin; validate live)
# ==========================================


def _duration_str(lookback_days: int) -> str:
    """IBKR rejects day-durations over 365 ("must be made in years"); express longer
    windows in whole years, rounded up so the indicator lookback stays covered."""
    if lookback_days > 365:
        return f"{math.ceil(lookback_days / 365)} Y"
    return f"{lookback_days} D"


async def fetch_daily_bars(ib: IB, ticker: str, lookback_days: int = 400) -> dict[str, dict]:
    """Daily TRADES bars for the trailing window (enough trailing history for the
    technical indicators). Returns our price-dict format."""
    contract = Stock(ticker, "SMART", "USD")
    await ib.qualifyContractsAsync(contract)
    bars = await ib.reqHistoricalDataAsync(
        contract, endDateTime="", durationStr=_duration_str(lookback_days),
        barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
    )
    return _bars_to_price_dict(bars or [])


async def refresh_price_cache(
    ib: IB, tickers: Iterable[str], *, lookback_days: int = 400,
    period: str = FORWARD_PRICE_PERIOD,
) -> int:
    """Ingest: pull daily bars for each ticker and write them to the on-disk price
    cache, so the existing sync price/technicals code reads IBKR data unchanged.
    Returns how many tickers were refreshed. One bad ticker doesn't abort the rest."""
    n = 0
    for t in tickers:
        try:
            bars = await fetch_daily_bars(ib, t, lookback_days)
            if bars:
                save_prices_to_cache(t, bars, period)
                n += 1
        except Exception as e:  # noqa: BLE001 — skip a name that fails, keep going
            logger.warning(f"IBKR price refresh failed for {t}: {e}")
    logger.info(f"IBKR price cache refreshed for {n} tickers (period={period})")
    return n


async def fetch_news_for(
    ib: IB, tickers: Iterable[str], as_of: str, *, lookback_days: int = 7,
    provider_codes: str | None = None, max_articles: int = 15,
) -> dict[str, list[dict]]:
    """Ingest recent news per ticker into a {ticker: [articles]} store (feed to
    news_source_from_store). Window is [as_of - lookback_days, as_of]. provider_codes
    defaults to all subscribed providers (e.g. Benzinga). Validate live."""
    if provider_codes is None:
        providers = await ib.reqNewsProvidersAsync()
        provider_codes = "+".join(p.code for p in providers) if providers else ""
    start = _window_start(as_of, lookback_days)
    store: dict[str, list[dict]] = {}
    for t in tickers:
        try:
            contract = Stock(t, "SMART", "USD")
            await ib.qualifyContractsAsync(contract)
            items = await ib.reqHistoricalNewsAsync(
                contract.conId, provider_codes, start, as_of, max_articles,
            )
            items = getattr(items, "articles", items) or []
            bodies: dict[str, str] = {}
            for it in items:
                try:
                    art = await ib.reqNewsArticleAsync(it.providerCode, it.articleId)
                    bodies[it.articleId] = getattr(art, "articleText", "") or ""
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"news body fetch failed for {t}/{it.articleId}: {e}")
            store[t] = _news_to_articles(items, bodies)
        except Exception as e:  # noqa: BLE001 — skip a name that fails, keep going
            logger.warning(f"IBKR news fetch failed for {t}: {e}")
            store[t] = []
    return store


def _window_start(as_of: str, lookback_days: int) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in as_of.split("-"))
    return (date(y, m, d) - timedelta(days=lookback_days)).isoformat()
