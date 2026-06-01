"""Fetch end-of-day OHLCV data from FMP and log to JSON."""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.data.fmp_client import get_historical_prices

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
UNIVERSE_FILE = Path("notes/universe.md")

# ==========================================
# 1. IMMUTABLE DATA MODEL
# ==========================================


@dataclass(frozen=True)
class OHLCV:
    open: float
    high: float
    low: float
    close: float
    volume: int


# ==========================================
# 2. PURE FUNCTIONS (Logic & Transforms)
# ==========================================


def parse_tickers(content: str) -> list[str]:
    """Pure function to extract tickers from markdown content."""
    return re.findall(r"^- \[([A-Z]{1,5})\]", content, flags=re.MULTILINE)


def transform_history_records(records: list[dict]) -> dict[str, dict]:
    """Pure function to map FMP's historical price list to our keyed dict format.

    FMP returns a list of {date, open, high, low, close, adjClose, volume, ...} dicts
    ordered most-recent first. We key by date string for fast lookup.
    """
    out: dict[str, dict] = {}
    for row in records:
        date_str = row["date"]  # FMP already formats as "YYYY-MM-DD"
        out[date_str] = asdict(OHLCV(
            open=round(float(row["open"]), 2),
            high=round(float(row["high"]), 2),
            low=round(float(row["low"]), 2),
            close=round(float(row["close"]), 2),
            volume=int(row["volume"]),
        ))
    return out


# ==========================================
# 3. I/O FUNCTIONS & CACHE MANAGEMENT
# ==========================================


def read_universe_file(filepath: Path) -> str:
    """Reads the universe markdown file."""
    if not filepath.exists():
        logger.warning(f"Universe file {filepath} not found.")
        return ""
    with filepath.open("r", encoding="utf-8") as f:
        return f.read()


def _period_years(period: str) -> int:
    """Years of history for a period string: 'max' (~35y, all FMP Premium offers),
    '<N>y' (e.g. '30y', '5y'), else 1. Cache keys include the period string, so
    different depths namespace cleanly on disk."""
    if period == "max":
        return 35
    if period.endswith("y"):
        try:
            return int(period[:-1])
        except ValueError:
            return 1
    return 1


def fetch_raw_history(ticker: str, period: str = "1y") -> list[dict]:
    """Fetches raw history from FMP, bounded to the last `period` window.

    Accepts "1y", "5y", "30y", "max", etc. Deep history (>5y) needs a paid FMP
    tier; the free tier serves ~5y and only for a limited symbol set.
    """
    today = datetime.now().date()
    years = _period_years(period)
    from_date = (today - timedelta(days=365 * years)).isoformat()
    to_date = today.isoformat()
    records = get_historical_prices(ticker, from_date=from_date, to_date=to_date)
    if not records:
        raise ValueError(f"No data returned for {ticker}")
    return records


def save_prices_to_cache(ticker: str, data: dict[str, dict], period: str = "1y") -> None:
    """Saves the transformed dictionary to disk, keyed by ticker + period + date.

    The period is part of the key so a shallow (1y) cache never silently
    satisfies a request that needs deeper (5y) history — point-in-time backtests
    over older dates require the deeper window.
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
# 4. EXPORTED DATA ACCESS API
# ==========================================


def process_ticker(ticker: str, period: str = "1y") -> dict[str, dict]:
    """Pipeline to forcibly Fetch -> Transform -> Cache."""
    logger.info(f"Fetching fresh data for {ticker} (period={period})...")
    raw = fetch_raw_history(ticker, period)
    records = transform_history_records(raw)
    save_prices_to_cache(ticker, records, period)
    return records


def get_prices(ticker: str, period: str = "1y") -> dict[str, dict]:
    """
    Primary data access method for downstream modules (technicals, etc.).
    Tries to load from cache; if missing, automatically fetches and caches.

    period: "1y" (default, live decisions) or "5y" (point-in-time backtests
    that need history before the earliest decision date plus indicator lookback).
    """
    records = load_latest_cache(ticker, period)
    if not records:
        logger.info(f"Cache miss for {ticker} (period={period}). Initiating fetch...")
        records = process_ticker(ticker, period)

    if not records:
        raise ValueError(f"Failed to retrieve or fetch price data for {ticker}")

    return records


def get_latest_close(ticker: str) -> tuple[str, float]:
    """
    Exported function for fundamentals.py.
    Now instantly fault-tolerant because it relies on get_prices().
    """
    records = get_prices(ticker)

    # YYYY-MM-DD strings sort perfectly, so max() gets the latest date
    latest_date = max(records.keys())
    latest_close = float(records[latest_date]["close"])

    return latest_date, latest_close


# ==========================================
# 5. ORCHESTRATOR
# ==========================================


def main() -> None:
    content = read_universe_file(UNIVERSE_FILE)
    tickers = parse_tickers(content)

    if not tickers:
        logger.error("No tickers found. Exiting.")
        return

    succeeded, failed = [], []

    for ticker in tickers:
        try:
            # We use process_ticker here to FORCE a daily refresh when run as a cron job
            data = process_ticker(ticker, period="1y")

            last_date = max(data.keys())
            last_close = data[last_date]["close"]

            logger.info(
                f"SUCCESS {ticker}: {len(data)} days, last close {last_close} on {last_date}"
            )
            succeeded.append(ticker)
        except Exception as e:
            logger.error(f"FAILED {ticker}: {e}")
            failed.append(ticker)

    logger.info(f"Done. {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        logger.warning(f"Failed Tickers: {failed}")


if __name__ == "__main__":
    main()
