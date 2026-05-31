"""
Dividend data access for total-return backtesting.

Reads the FMP dividend cache grabbed under data/cache/fmp_signals/{TICKER}_dividends.json
(list of {date, dividend, adjDividend, ...} where `date` is the ex-dividend date).

Returns the **split-adjusted** per-share dividend (`adjDividend`), keyed by ex-date.
Our cached price series is split-adjusted (verified: NVDA shows no discontinuity at
its 2024 10:1 split) but NOT dividend-adjusted, so dividends are a real, separate
cash flow — and adjDividend is on the same split basis as the prices, so
shares-at-fill × adjDividend is consistent. (FMP's `close` is split-adjusted only;
`adjClose`, the total-return series, was not stored — so there is no double-count.)

Offline/cache-only: an unavailable ticker returns {} (no dividends modeled), never a
live fetch — consistent with the rest of the sp500 backtest path.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_SIGNALS_DIR = Path("data/cache/fmp_signals")


@lru_cache(maxsize=2048)
def load_dividends(ticker: str) -> dict[str, float]:
    """{ex_date (YYYY-MM-DD): split-adjusted dividend per share} for a ticker.

    Empty dict if no dividend file exists (non-payers, or names we didn't grab)."""
    path = _SIGNALS_DIR / f"{ticker}_dividends.json"
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001 — a corrupt file is just "no dividends"
        logger.warning(f"dividend cache read failed for {ticker}: {e}. Treating as none.")
        return {}
    out: dict[str, float] = {}
    for row in rows:
        ex_date = row.get("date")
        amount = row.get("adjDividend", row.get("dividend"))
        if ex_date and amount:
            out[ex_date] = float(amount)
    return out
