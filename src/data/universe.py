"""
Point-in-time S&P 500 membership — the bot's eligible universe.

FMP's constituent endpoints are paywalled, so we source membership from the
community-maintained `fja05680/sp500` dataset (free). We use its
`sp500_ticker_start_end.csv` — one row per (ticker, membership spell) with a
start and (optional) end date — which makes as-of reconstruction trivial and
handles tickers that left and re-entered the index.

`sp500_as_of(date)` is the eligible universe on a given date (point-in-time, so
backtests are not survivorship-biased). `current_sp500()` is for live use.

Caveats (see notes/universe-design.md):
- The dataset's coverage ends on its last update; index changes after that are
  not reflected (refresh the cached CSV periodically).
- Historical tickers use the symbol as-of then; reconciling renames (FB->META)
  against EDGAR/price symbols is a known edge for deep-history windows.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SOURCE_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)
REF_DIR = Path("data/reference")
CSV_PATH = REF_DIR / "sp500_ticker_start_end.csv"

_FUTURE = "9999-12-31"  # sentinel for still-a-member (empty end_date)


def _ensure_csv() -> Path:
    if not CSV_PATH.exists():
        REF_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading S&P 500 historical membership CSV...")
        resp = requests.get(SOURCE_URL, timeout=30)
        resp.raise_for_status()
        CSV_PATH.write_bytes(resp.content)
    return CSV_PATH


def _load_membership() -> list[tuple[str, str, str]]:
    """Returns (ticker, start_date, end_date) rows; empty end_date -> still a member."""
    out: list[tuple[str, str, str]] = []
    with _ensure_csv().open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = (row.get("ticker") or "").strip()
            start = (row.get("start_date") or "").strip()
            end = (row.get("end_date") or "").strip() or _FUTURE
            if ticker and start:
                out.append((ticker, start, end))
    return out


def sp500_as_of(date: str) -> list[str]:
    """Sorted S&P 500 tickers that were index members on `date` (YYYY-MM-DD).

    A ticker is a member if start_date <= date <= end_date (membership spell).
    Tickers with multiple spells are handled (any covering spell qualifies).
    """
    members = {
        ticker
        for ticker, start, end in _load_membership()
        if start <= date <= end
    }
    return sorted(members)


def sp500_members_in_range(start_date: str, end_date: str) -> list[str]:
    """Sorted tickers that were S&P 500 members at any point in [start_date, end_date].

    A membership spell [s, e] overlaps the window iff s <= end_date and e >= start_date.
    This is the exact set of names a backtest over the window can touch — including
    ones that left mid-window — so it's what the backfill should seed (vs a single
    as-of snapshot, which misses names that dropped out before the end date).
    """
    members = {
        ticker
        for ticker, start, end in _load_membership()
        if start <= end_date and end >= start_date
    }
    return sorted(members)


def membership_end(ticker: str) -> str | None:
    """Latest membership end_date for `ticker` IF it is not a current member, else None.

    Used to truncate a delisted name's price series: ticker symbols get recycled
    after delisting (e.g. SBNY post-Signature), so a symbol-keyed price lookup can
    return a *different company's* prices after the delisting date. Truncating at
    the membership end keeps the series clean (and beyond it the name isn't eligible
    anyway). Returns None for current members (their series is legitimately live).
    """
    spells = [(s, e) for t, s, e in _load_membership() if t == ticker.upper()]
    if not spells:
        return None
    if any(end == _FUTURE for _, end in spells):
        return None  # still a member
    return max(end for _, end in spells)


def current_sp500() -> list[str]:
    """Current S&P 500 membership (for live use), as of today."""
    return sp500_as_of(dt.date.today().isoformat())
