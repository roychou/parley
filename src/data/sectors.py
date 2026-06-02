"""
Ticker -> sector, from FMP company profiles, for the risk layer's concentration cap.

Sectors change rarely, so the map is cached on disk (data/reference/sectors.json) and
memoized in-process. A miss fetches the FMP profile once; a failure returns None and
the name is simply left out of sector capping rather than blocking sizing.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_PATH = Path("data/reference/sectors.json")
_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        _cache = json.loads(_CACHE_PATH.read_text()) if _CACHE_PATH.exists() else {}
    return _cache


def sector_of(ticker: str) -> str | None:
    """The ticker's sector (FMP profile), cached. None if unknown or the fetch fails —
    the name is then exempt from sector capping rather than blocking the run."""
    cache = _load()
    if ticker in cache:
        return cache[ticker] or None

    from src.data import fmp_client
    try:
        data = fmp_client._get("profile", {"symbol": ticker})
    except fmp_client.FMPError as e:
        logger.warning(f"sector lookup failed for {ticker}: {e}")
        return None  # transient — don't cache, retry next time

    row = data[0] if isinstance(data, list) and data else {}
    sec = row.get("sector") or ""
    cache[ticker] = sec  # cache even an empty result to avoid refetching unknowns
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))
    return sec or None


def sector_map(tickers) -> dict[str, str]:
    """{ticker: sector} for the tickers that resolve to a known sector."""
    out: dict[str, str] = {}
    for t in tickers:
        s = sector_of(t)
        if s:
            out[t] = s
    return out
