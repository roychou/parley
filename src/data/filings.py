"""
Recent filing dates for the event-driven screen — EDGAR first, FMP fallback.

The screen triggers a name when it filed within the trailing window. EDGAR covers
US-GAAP quarterly filers; foreign private issuers (20-F/40-F: ASML, PDD, Ferrovial,
…) have no such filings, so they'd never trigger and could only ever enter the
universe via holdings. This composes EDGAR with FMP's income-statement filing dates
so every name's earnings can trigger the screen.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def recent_filing_dates(ticker: str, forms: tuple[str, ...] = ("10-Q", "10-K")) -> list[str]:
    """Filing dates (YYYY-MM-DD) for `ticker`, from EDGAR; falls back to FMP's
    income-statement `filingDate`s when EDGAR has none (foreign filers). Empty list if
    neither source resolves — the name simply isn't screened in this period."""
    from src.data.edgar import EdgarError
    from src.data.edgar import recent_filing_dates as edgar_filing_dates
    try:
        dates = edgar_filing_dates(ticker, forms)
    except EdgarError:
        dates = []
    if dates:
        return dates

    # FMP fallback (foreign 20-F/40-F filers EDGAR can't serve).
    from src.data import fmp_client
    try:
        rows = fmp_client._get(
            "income-statement", {"symbol": ticker, "period": "quarter", "limit": 8}
        )
    except fmp_client.FMPError as e:
        logger.warning(f"FMP filing-date fallback failed for {ticker}: {e}")
        return []
    if not isinstance(rows, list):
        return []
    return [r["filingDate"] for r in rows if isinstance(r, dict) and r.get("filingDate")]
