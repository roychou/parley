import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.data.fetch_prices import get_latest_close
from src.data.fmp_client import FMPError, get_balance_sheet, get_income_statement

# --- Logging & Config ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache/fundamentals")
UNIVERSE_FILE = Path("notes/universe.md")


# --- Immutable Data Model ---
@dataclass(frozen=True)
class ValuationSnapshot:
    """Point-in-time-anchored fundamentals snapshot.

    report_date semantics: the actual SEC filing date (FMP `acceptedDate`).
    Backtests must use this — not period_end_date — as the availability anchor,
    since the data was not actually published until report_date.
    """
    price_date: str
    report_date: str         # filing date (when data became publicly available)
    period_end_date: str     # fiscal period-end date the filing covers
    diluted_eps: float
    profit_margin: float
    rev_growth_yoy: float
    debt_to_equity: float
    pe_ratio: float


# ==========================================
# 1. PURE FUNCTIONS (Math & Logic)
# ==========================================


def _is_nan(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def calc_pe(price: float, eps: float) -> float:
    if _is_nan(eps) or _is_nan(price) or eps <= 0:
        return float("nan")
    return float(price / eps)


def calc_margin(net_income: float, revenue: float) -> float:
    if _is_nan(revenue) or revenue == 0:
        return float("nan")
    return float(net_income / revenue)


def calc_growth_yoy(current: float, previous: float) -> float:
    if _is_nan(previous) or previous == 0:
        return float("nan")
    return float((current - previous) / previous)


def calc_debt_equity(debt: float, equity: float) -> float:
    if _is_nan(equity) or equity == 0:
        return float("nan")
    return float(debt / equity)


def _safe_float(value: Any) -> float:
    """Coerce FMP-returned values to float, defaulting to NaN for missing/invalid."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# ==========================================
# 2. I/O FUNCTIONS & CACHE MANAGEMENT
# ==========================================


def fetch_fmp_raw_fundamentals(ticker: str) -> dict:
    """Fetches data from FMP and returns a raw dictionary.

    Uses the most recent annual filing. Returns both `report_date` (actual
    filing date, FMP `acceptedDate`) and `period_end_date` (fiscal period close).
    """
    income = get_income_statement(ticker, limit=2)
    balance = get_balance_sheet(ticker, limit=1)

    if len(income) < 2:
        raise FMPError(
            f"Need at least two annual income statements for {ticker} to compute YoY growth; got {len(income)}"
        )

    return _build_raw_from_filings(
        latest_income=income[0],
        prior_income=income[1],
        latest_balance=balance[0],
    )


def fetch_fmp_all_filings_raw(ticker: str, limit: int = 5) -> list[dict]:
    """Returns a list of point-in-time raw fundamentals dicts, most recent first.

    Each entry has the same shape as `fetch_fmp_raw_fundamentals` returns, but
    represents a single historical filing rather than just the latest. The list
    contains up to N-1 entries when N income statements are available (the oldest
    is used only as the YoY prior-revenue reference for the next-oldest filing).

    `limit` is capped to 5 by FMP's free tier. Five annual filings cover a 4-year
    backtest window (the oldest is the YoY-reference for the next-oldest).

    Used by `get_fundamentals_as_of` for point-in-time backtest replay.
    """
    income = get_income_statement(ticker, limit=min(limit, 5))
    balance = get_balance_sheet(ticker, limit=min(limit, 5))

    if len(income) < 2:
        raise FMPError(
            f"Need at least two annual income statements for {ticker} to build a filing history; got {len(income)}"
        )

    balance_by_period = {b["date"]: b for b in balance}

    out: list[dict] = []
    for i in range(len(income) - 1):
        latest_income = income[i]
        prior_income = income[i + 1]
        latest_balance = balance_by_period.get(latest_income["date"])
        if latest_balance is None:
            # No matching balance sheet for this period; skip
            continue
        out.append(_build_raw_from_filings(latest_income, prior_income, latest_balance))
    return out


def _build_raw_from_filings(latest_income: dict, prior_income: dict, latest_balance: dict) -> dict:
    """Pure builder: combines paired FMP filing dicts into our raw fundamentals shape."""
    # FMP returns acceptedDate as "YYYY-MM-DD HH:MM:SS"; strip to date.
    # filingDate is a clean "YYYY-MM-DD" — prefer it if present.
    filing = latest_income.get("filingDate") or latest_income.get("acceptedDate") or latest_income.get("date")
    report_date = filing.split(" ")[0] if isinstance(filing, str) else filing

    diluted_eps = _safe_float(latest_income.get("epsDiluted"))
    net_income = _safe_float(latest_income.get("netIncome"))
    curr_revenue = _safe_float(latest_income.get("revenue"))
    prev_revenue = _safe_float(prior_income.get("revenue"))
    total_debt = _safe_float(latest_balance.get("totalDebt"))
    equity = _safe_float(latest_balance.get("totalStockholdersEquity"))

    return {
        "report_date": report_date,
        "period_end_date": latest_income.get("date"),
        "diluted_eps": diluted_eps,
        "profit_margin": calc_margin(net_income, curr_revenue),
        "rev_growth_yoy": calc_growth_yoy(curr_revenue, prev_revenue),
        "debt_to_equity": calc_debt_equity(total_debt, equity),
    }


# ==========================================
# POINT-IN-TIME: filings history cache + as-of lookup
# ==========================================


FILINGS_CACHE_DIR = CACHE_DIR.parent / "filings_history"


def _filings_cache_path(ticker: str) -> Path:
    FILINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    return FILINGS_CACHE_DIR / f"{ticker}_{today_str}.json"


def _load_filings_cache(ticker: str) -> list[dict] | None:
    FILINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    matches = sorted(FILINGS_CACHE_DIR.glob(f"{ticker}_*.json"))
    if not matches:
        return None
    try:
        with matches[-1].open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def _save_filings_cache(ticker: str, filings: list[dict]) -> None:
    path = _filings_cache_path(ticker)
    with path.open("w", encoding="utf-8") as f:
        json.dump(filings, f, indent=2)


def get_filings_history(ticker: str) -> list[dict]:
    """Returns the full list of point-in-time raw filings for a ticker.

    Uses an on-disk cache (keyed by ticker + today's date) so repeated calls
    within the same day don't hit FMP. Cache files persist across days; the
    most recent file is preferred.
    """
    cached = _load_filings_cache(ticker)
    if cached is not None:
        return cached
    filings = fetch_fmp_all_filings_raw(ticker)
    _save_filings_cache(ticker, filings)
    return filings


def get_fundamentals_as_of(ticker: str, as_of_date: str) -> Optional[ValuationSnapshot]:
    """Returns the most recent filing available as of `as_of_date`, with P/E computed
    using the close price on (or most recently before) `as_of_date`.

    Returns None if no filing has report_date <= as_of_date, or if no price is available.
    """
    filings = get_filings_history(ticker)
    eligible = [f for f in filings if f["report_date"] and f["report_date"] <= as_of_date]
    if not eligible:
        return None
    latest = eligible[0]  # filings are most-recent first

    # Get the close price at as_of_date (or the most recent available before it)
    prices = _get_prices_dict(ticker)
    eligible_price_dates = sorted(d for d in prices if d <= as_of_date)
    if not eligible_price_dates:
        return None
    price_date = eligible_price_dates[-1]
    price = float(prices[price_date]["close"])

    return ValuationSnapshot(
        price_date=price_date,
        report_date=latest["report_date"],
        period_end_date=latest["period_end_date"],
        diluted_eps=latest["diluted_eps"],
        profit_margin=latest["profit_margin"],
        rev_growth_yoy=latest["rev_growth_yoy"],
        debt_to_equity=latest["debt_to_equity"],
        pe_ratio=calc_pe(price, latest["diluted_eps"]),
    )


def _get_prices_dict(ticker: str) -> dict:
    """Import-late helper to avoid circular import with fetch_prices."""
    from src.data.fetch_prices import get_prices
    return get_prices(ticker)


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
    logger.info(f"Fetching fresh FMP fundamentals for {ticker}...")

    try:
        latest_price_date, current_price = get_latest_close(ticker)
    except Exception as e:
        raise RuntimeError(f"Cannot build snapshot. Failed to load prices for {ticker}: {e}")

    raw_funds = fetch_fmp_raw_fundamentals(ticker)

    snapshot = ValuationSnapshot(
        price_date=latest_price_date,
        report_date=raw_funds["report_date"],
        period_end_date=raw_funds["period_end_date"],
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


# ==========================================
# 4. ORCHESTRATOR
# ==========================================


def main() -> None:
    ticker = "MSFT"
    try:
        snapshot = process_ticker(ticker)
        logger.info(f"Snapshot for {ticker}: {snapshot}")
    except Exception as e:
        logger.error(f"Pipeline failed for {ticker}: {e}")


if __name__ == "__main__":
    main()
