import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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


def pe_band(pe: float | None) -> str:
    """Coarse P/E band matching the fundamentals prompt's thresholds
    ("P/E above 40 is high, below 15 is low").

    Used to key the fundamentals signal cache: the specialist reasons about P/E
    only via these thresholds, so within a band its signal is stable and the
    cached analysis can be reused until P/E crosses a boundary (or a new filing
    lands). This is what lets fundamentals refresh ~quarterly instead of daily.
    """
    if _is_nan(pe) or pe is None or pe <= 0:
        return "na"
    if pe < 15:
        return "low"
    if pe <= 40:
        return "fair"
    return "high"


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


# ==========================================
# POINT-IN-TIME: filings history cache + as-of lookup
# ==========================================


FILINGS_CACHE_DIR = CACHE_DIR.parent / "filings_history"
# Part of the cache key so a change to the extraction logic doesn't serve stale
# filings (the cache is otherwise keyed only by ticker+date). Bump when
# build_filings_history changes.
FILINGS_CACHE_VERSION = "edgar-v2"  # v2: broadened REVENUE_CONCEPTS (gross/bank tags)


def _filings_cache_path(ticker: str) -> Path:
    FILINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d")
    return FILINGS_CACHE_DIR / f"{ticker}_{FILINGS_CACHE_VERSION}_{today_str}.json"


def _load_filings_cache(ticker: str) -> list[dict] | None:
    FILINGS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    matches = sorted(FILINGS_CACHE_DIR.glob(f"{ticker}_{FILINGS_CACHE_VERSION}_*.json"))
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
    """Returns the full list of point-in-time raw filings for a ticker (from EDGAR).

    Uses an on-disk cache keyed by ticker + extraction version + date, so repeated
    calls within the same day don't refetch and a version bump invalidates stale
    extractions. Cache files persist across days; the most recent is preferred.
    """
    cached = _load_filings_cache(ticker)
    if cached is not None:
        return cached
    # Point-in-time fundamentals come from SEC EDGAR (deep history, true filing
    # dates, real quarterly YoY). Late import avoids a circular dependency.
    from src.data.edgar import build_filings_history
    filings = build_filings_history(ticker)
    _save_filings_cache(ticker, filings)
    return filings


def get_fundamentals_as_of(
    ticker: str, as_of_date: str, price_period: str = "5y"
) -> Optional[ValuationSnapshot]:
    """Returns the most recent filing available as of `as_of_date`, with P/E computed
    using the close price on (or most recently before) `as_of_date`.

    Returns None if no filing has report_date <= as_of_date, if no price is
    available, or if the ticker doesn't resolve on EDGAR (e.g. no CIK in the
    current SEC map). At index scale one unresolvable name must not abort the
    pre-load of the whole universe — the replay loop treats None as "skip".
    price_period defaults to "5y" because backtest as-of dates can sit well before
    today; a 1y price window would return None for older decision dates.
    """
    from src.data.edgar import EdgarError
    try:
        filings = get_filings_history(ticker)
    except EdgarError:
        return None
    eligible = [f for f in filings if f["report_date"] and f["report_date"] <= as_of_date]
    if not eligible:
        return None
    latest = eligible[0]  # filings are most-recent first

    # Get the close price at as_of_date (or the most recent available before it)
    prices = _get_prices_dict(ticker, price_period)
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


def _get_prices_dict(ticker: str, period: str = "5y") -> dict:
    """Import-late helper to avoid circular import with fetch_prices."""
    from src.data.fetch_prices import get_prices
    return get_prices(ticker, period)


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
    """Live fetch: build the latest snapshot from SEC EDGAR (same source as the
    backtest path) and cache it. 1y price window is enough for today's P/E."""
    logger.info(f"Fetching fresh EDGAR fundamentals for {ticker}...")
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot = get_fundamentals_as_of(ticker, today, price_period="1y")
    if snapshot is None:
        raise RuntimeError(f"No fundamentals available for {ticker} as of {today}")
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
