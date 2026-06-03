"""
Recent filing dates for the event-driven screen — EDGAR only.

The screen triggers a name when it filed within the trailing window. EDGAR covers
US-GAAP quarterly filers (10-Q/10-K) and foreign private issuers' annual reports
(20-F/40-F: ASML, PDD, Ferrovial, …), so every name's earnings can trigger the
screen without a paid data vendor.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 10-Q/10-K cover US domestic filers; 20-F/40-F cover foreign private issuers.
DEFAULT_FORMS = ("10-Q", "10-K", "20-F", "40-F")


def recent_filing_dates(ticker: str, forms: tuple[str, ...] = DEFAULT_FORMS) -> list[str]:
    """Filing dates (YYYY-MM-DD) for `ticker`, from EDGAR. Empty list if EDGAR has none
    — the name simply isn't screened in this period."""
    from src.data.edgar import EdgarError
    from src.data.edgar import recent_filing_dates as edgar_filing_dates
    try:
        return edgar_filing_dates(ticker, forms)
    except EdgarError as e:
        logger.warning(f"EDGAR filing dates failed for {ticker}: {e}")
        return []
