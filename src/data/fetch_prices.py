"""End-of-day OHLCV price cache.

Prices are sourced from IBKR (see `src/forward/ibkr.py`, which fetches bars and
writes them here via `save_prices_to_cache`). This module is now a pure cache layer:
it persists and serves price dicts, with no external data-vendor dependency. A cache
miss is an error — there is no live fetch fallback.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

# ==========================================
# 0. LOGGING & CONFIG
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")

# ==========================================
# 1. CACHE I/O
# ==========================================


def save_prices_to_cache(ticker: str, data: dict[str, dict], period: str = "1y") -> None:
    """Saves the transformed dictionary to disk, keyed by ticker + period + date.

    The period is part of the key so a shallow (1y) cache never silently
    satisfies a request that needs deeper history.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    out_path = CACHE_DIR / f"{ticker}_{period}_{today_str}.json"

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_latest_cache(ticker: str, period: str = "1y") -> dict[str, dict] | None:
    """Loads the most recently cached JSON for a given ticker at the given period depth."""
    if not CACHE_DIR.exists():
        return None

    matches = sorted(CACHE_DIR.glob(f"{ticker}_{period}_*.json"))
    if not matches:
        return None

    try:
        with matches[-1].open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse cache file for {ticker}: {e}")
        return None


# ==========================================
# 2. EXPORTED DATA ACCESS API
# ==========================================


def get_prices(ticker: str, period: str = "1y") -> dict[str, dict]:
    """Primary read API for downstream modules (technicals, fundamentals P/E).

    Cache-only: prices must already have been warmed into the cache (the forward
    clock does this via the IBKR refresh, period="ibkr"). A miss raises rather than
    silently fetching — there is no vendor fallback.
    """
    records = load_latest_cache(ticker, period)
    if not records:
        raise ValueError(
            f"No cached price data for {ticker} (period={period}). "
            f"Warm the cache from IBKR before requesting prices."
        )
    return records
