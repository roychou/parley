"""Fetch end-of-day OHLCV data from yfinance and log to JSON."""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

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


def parse_tickers(content: str) -> List[str]:
    """Pure function to extract tickers from markdown content."""
    return re.findall(r"^- \[([A-Z]{1,5})\]", content, flags=re.MULTILINE)


def transform_history_df(df: pd.DataFrame) -> Dict[str, dict]:
    """Pure function to map a yfinance DataFrame to our JSON-ready dictionary format."""
    records = {}
    for date, row in df.iterrows():
        date_str = date.strftime("%Y-%m-%d")

        row_data = OHLCV(
            open=round(float(row["Open"]), 2),
            high=round(float(row["High"]), 2),
            low=round(float(row["Low"]), 2),
            close=round(float(row["Close"]), 2),
            volume=int(row["Volume"]),
        )
        records[date_str] = asdict(row_data)

    return records


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


def fetch_raw_history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """Fetches raw history from Yahoo Finance."""
    t = yf.Ticker(ticker)
    df = t.history(period=period)
    if df.empty:
        raise ValueError(f"No data returned for {ticker}")
    return df


def save_prices_to_cache(ticker: str, data: Dict[str, dict]) -> None:
    """Saves the transformed dictionary to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    out_path = CACHE_DIR / f"{ticker}_{today_str}.json"

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_latest_cache(ticker: str) -> Optional[Dict[str, dict]]:
    """Loads the most recently cached JSON for a given ticker."""
    if not CACHE_DIR.exists():
        return None

    matches = sorted(CACHE_DIR.glob(f"{ticker}_*.json"))
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


def process_ticker(ticker: str, period: str = "1y") -> Dict[str, dict]:
    """Pipeline to forcibly Fetch -> Transform -> Cache."""
    logger.info(f"Fetching fresh data for {ticker}...")
    df = fetch_raw_history(ticker, period)
    records = transform_history_df(df)
    save_prices_to_cache(ticker, records)
    return records


def get_prices(ticker: str) -> Dict[str, dict]:
    """
    Primary data access method for downstream modules (technicals, etc.).
    Tries to load from cache; if missing, automatically fetches and caches.
    """
    records = load_latest_cache(ticker)
    if not records:
        logger.info(f"Cache miss for {ticker}. Initiating fetch...")
        records = process_ticker(ticker)

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
