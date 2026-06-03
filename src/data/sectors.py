"""
Ticker -> sector for the risk layer's per-sector concentration cap.

A committed reference map for the eligible universe — sectors are stable facts (like
index membership), so they live here as data, with no live API dependency. A ticker
not in the map (e.g. a brand-new constituent) returns None and is simply exempt from
the sector cap until added; refresh when the index reconstitutes (annually). Seeded
from company-profile sector classifications.
"""
from __future__ import annotations

# GICS-style sector per ticker. Extend when the universe changes.
_SECTORS: dict[str, str] = {
    "AAPL": "Technology",
    "ABNB": "Consumer Cyclical",
    "ADBE": "Technology",
    "ADI": "Technology",
    "ADP": "Industrials",
    "ADSK": "Technology",
    "AEP": "Utilities",
    "ALNY": "Healthcare",
    "AMAT": "Technology",
    "AMD": "Technology",
    "AMGN": "Healthcare",
    "AMZN": "Consumer Cyclical",
    "APP": "Technology",
    "ARM": "Technology",
    "ASML": "Technology",
    "AVGO": "Technology",
    "AXON": "Industrials",
    "BKNG": "Consumer Cyclical",
    "BKR": "Energy",
    "CALM": "Consumer Defensive",
    "CCEP": "Consumer Defensive",
    "CDNS": "Technology",
    "CEG": "Utilities",
    "CHTR": "Communication Services",
    "CMCSA": "Communication Services",
    "COST": "Consumer Defensive",
    "CPRT": "Industrials",
    "CRWD": "Technology",
    "CSCO": "Technology",
    "CSGP": "Real Estate",
    "CSX": "Industrials",
    "CTAS": "Industrials",
    "CTSH": "Technology",
    "DASH": "Communication Services",
    "DDOG": "Technology",
    "DXCM": "Healthcare",
    "EA": "Communication Services",
    "EXC": "Utilities",
    "FANG": "Energy",
    "FAST": "Industrials",
    "FER": "Industrials",
    "FTNT": "Technology",
    "GEHC": "Healthcare",
    "GILD": "Healthcare",
    "GOOG": "Communication Services",
    "GOOGL": "Communication Services",
    "HON": "Industrials",
    "IDXX": "Healthcare",
    "INSM": "Healthcare",
    "INTC": "Technology",
    "INTU": "Technology",
    "ISRG": "Healthcare",
    "KDP": "Consumer Defensive",
    "KHC": "Consumer Defensive",
    "KLAC": "Technology",
    "LIN": "Basic Materials",
    "LRCX": "Technology",
    "MAR": "Consumer Cyclical",
    "MCHP": "Technology",
    "MDLZ": "Consumer Defensive",
    "MELI": "Consumer Cyclical",
    "META": "Communication Services",
    "MNST": "Consumer Defensive",
    "MPWR": "Technology",
    "MRVL": "Technology",
    "MSFT": "Technology",
    "MSTR": "Technology",
    "MU": "Technology",
    "NFLX": "Communication Services",
    "NVDA": "Technology",
    "NXPI": "Technology",
    "ODFL": "Industrials",
    "ORLY": "Consumer Cyclical",
    "PANW": "Technology",
    "PAYX": "Industrials",
    "PCAR": "Industrials",
    "PDD": "Consumer Cyclical",
    "PEP": "Consumer Defensive",
    "PLTR": "Technology",
    "PYPL": "Financial Services",
    "QCOM": "Technology",
    "REGN": "Healthcare",
    "ROP": "Technology",
    "ROST": "Consumer Cyclical",
    "SBUX": "Consumer Cyclical",
    "SHOP": "Technology",
    "SNPS": "Technology",
    "STX": "Technology",
    "TEAM": "Technology",
    "TMUS": "Communication Services",
    "TRI": "Industrials",
    "TSLA": "Consumer Cyclical",
    "TTWO": "Communication Services",
    "TXN": "Technology",
    "VRSK": "Technology",
    "VRTX": "Healthcare",
    "WBD": "Communication Services",
    "WDAY": "Technology",
    "WDC": "Technology",
    "WMT": "Consumer Defensive",
    "XEL": "Utilities",
    "ZS": "Technology",
}


def sector_of(ticker: str) -> str | None:
    """The ticker's sector, or None if unmapped (then exempt from the sector cap)."""
    return _SECTORS.get(ticker) or None


def sector_map(tickers) -> dict[str, str]:
    """{ticker: sector} for the tickers that resolve to a known sector."""
    out: dict[str, str] = {}
    for t in tickers:
        s = sector_of(t)
        if s:
            out[t] = s
    return out
