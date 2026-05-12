import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

# IMPORT FIX: Grab the fault-tolerant price function
from src.data.fetch_prices import get_latest_close

# --- Logging & Config ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache/fundamentals")
UNIVERSE_FILE = Path("notes/universe.md")


# --- Immutable Data Model ---
@dataclass(frozen=True)
class ValuationSnapshot:
    price_date: str
    report_date: str
    diluted_eps: float
    profit_margin: float
    rev_growth_yoy: float
    debt_to_equity: float
    pe_ratio: float


# ==========================================
# 1. PURE FUNCTIONS (Math & Logic)
# ==========================================


def calc_pe(price: float, eps: float) -> float:
    if pd.isna(eps) or eps <= 0 or pd.isna(price):
        return float("nan")
    return float(price / eps)


def calc_margin(net_income: float, revenue: float) -> float:
    if pd.isna(revenue) or revenue == 0:
        return float("nan")
    return float(net_income / revenue)


def calc_growth_yoy(current: float, previous: float) -> float:
    if pd.isna(previous) or previous == 0:
        return float("nan")
    return float((current - previous) / previous)


def calc_debt_equity(debt: float, equity: float) -> float:
    if pd.isna(equity) or equity == 0:
        return float("nan")
    return float(debt / equity)


def safe_get_metric(df: pd.DataFrame, index_name: str, col_idx: int = 0) -> float:
    """Pure helper to safely extract a metric from a DataFrame."""
    try:
        if index_name in df.index:
            val = df.loc[index_name].iloc[col_idx]
            return float(val) if pd.notna(val) else float("nan")
        return float("nan")
    except (IndexError, KeyError):
        return float("nan")


# ==========================================
# 2. I/O FUNCTIONS & CACHE MANAGEMENT
# ==========================================


def fetch_yfinance_raw_fundamentals(ticker: str) -> dict:
    """Fetches data from Yahoo Finance and returns a raw dictionary."""
    t = yf.Ticker(ticker)
    financials = t.financials
    balance_sheet = t.balance_sheet

    if financials.empty or balance_sheet.empty:
        raise ValueError(f"No financial data returned for {ticker}")

    diluted_eps = safe_get_metric(financials, "Diluted EPS", 0)
    net_income = safe_get_metric(financials, "Net Income", 0)
    curr_revenue = safe_get_metric(financials, "Total Revenue", 0)
    prev_revenue = safe_get_metric(financials, "Total Revenue", 1)
    total_debt = safe_get_metric(balance_sheet, "Total Debt", 0)
    equity = safe_get_metric(balance_sheet, "Stockholders Equity", 0)

    return {
        "report_date": financials.columns[0].strftime("%Y-%m-%d"),
        "diluted_eps": diluted_eps,
        "profit_margin": calc_margin(net_income, curr_revenue),
        "rev_growth_yoy": calc_growth_yoy(curr_revenue, prev_revenue),
        "debt_to_equity": calc_debt_equity(total_debt, equity),
    }


def save_snapshot_to_cache(ticker: str, snapshot: ValuationSnapshot) -> None:
    """Saves the typed dataclass to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    path = CACHE_DIR / f"{ticker}_{today_str}.json"

    payload = {snapshot.price_date: asdict(snapshot)}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.debug(f"Cached valuation snapshot for {ticker} at {path}")


def load_latest_cache(ticker: str) -> Optional[ValuationSnapshot]:
    """Loads the most recently cached fundamentals directly into the dataclass."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    matches = sorted(CACHE_DIR.glob(f"{ticker}_*.json"))
    if not matches:
        return None

    try:
        with matches[-1].open("r", encoding="utf-8") as f:
            data = json.load(f)
            if data:
                _, metrics_dict = next(iter(data.items()))
                return ValuationSnapshot(**metrics_dict)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse cache file for {ticker}: {e}")
    return None


# ==========================================
# 3. EXPORTED DATA ACCESS API
# ==========================================


def process_ticker(ticker: str) -> ValuationSnapshot:
    """Pipeline to forcefully Fetch -> Compute -> Cache -> Return."""
    logger.info(f"Fetching fresh yfinance fundamentals for {ticker}...")

    try:
        latest_price_date, current_price = get_latest_close(ticker)
    except Exception as e:
        raise RuntimeError(f"Cannot build snapshot. Failed to load prices for {ticker}: {e}")

    raw_funds = fetch_yfinance_raw_fundamentals(ticker)

    snapshot = ValuationSnapshot(
        price_date=latest_price_date,
        report_date=raw_funds["report_date"],
        diluted_eps=raw_funds["diluted_eps"],
        profit_margin=raw_funds["profit_margin"],
        rev_growth_yoy=raw_funds["rev_growth_yoy"],
        debt_to_equity=raw_funds["debt_to_equity"],
        pe_ratio=calc_pe(current_price, raw_funds["diluted_eps"]),
    )

    save_snapshot_to_cache(ticker, snapshot)
    return snapshot


def get_fundamentals(ticker: str) -> ValuationSnapshot:
    """
    Primary data access method for MCP Servers and Agents.
    Tries cache first; automatically falls back to live fetch if missing.
    """
    snapshot = load_latest_cache(ticker)
    if not snapshot:
        logger.info(f"Cache miss for {ticker} fundamentals. Initiating fetch...")
        snapshot = process_ticker(ticker)

    if not snapshot:
        raise ValueError(f"Failed to retrieve or fetch fundamentals for {ticker}")

    return snapshot


def load_snapshot_df(ticker: str) -> pd.DataFrame:
    """Utility function: seamlessly fetches fault-tolerant data and returns a DataFrame."""
    snapshot = get_fundamentals(ticker)  # Now completely immune to cache misses!
    df = pd.DataFrame.from_dict({snapshot.price_date: asdict(snapshot)}, orient="index")
    df.index = pd.to_datetime(df.index, format="%Y-%m-%d")
    return df


# ==========================================
# 4. ORCHESTRATOR
# ==========================================


def main() -> None:
    ticker = "MSFT"
    try:
        # Calling process_ticker explicitly forces a fresh API pull
        process_ticker(ticker)

        # Checking DataFrame transformation
        df = load_snapshot_df(ticker)
        logger.info(f"\n{df}")
    except Exception as e:
        logger.error(f"Pipeline failed for {ticker}: {e}")


if __name__ == "__main__":
    main()
