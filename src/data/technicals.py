from dataclasses import dataclass

import pandas as pd

# IMPORT FIX: Grab the process_ticker function and alias it so it doesn't clash
from src.data.fetch_prices import get_prices

# ==========================================
# 1. INDICATOR MATH
# ==========================================


def sma(prices: list[float] | pd.Series, window: int) -> pd.Series:
    """Calculates the Simple Moving Average (SMA)."""
    if isinstance(prices, list):
        prices = pd.Series(prices, dtype=float)
    return prices.rolling(window=window).mean()


def rsi(prices: list[float] | pd.Series, window: int = 14) -> pd.Series:
    """
    Calculates the Relative Strength Index (RSI).

    RSI is a momentum indicator used in technical analysis. It measures the
    speed and magnitude of a security's recent price changes to detect
    overbought or oversold conditions. Displayed on a scale of 0 to 100.
    """
    if isinstance(prices, list):
        prices = pd.Series(prices, dtype=float)

    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ==========================================
# 2. DATA PROCESSING & EXPORT
# ==========================================


@dataclass
class TechnicalsSnapshot:
    """Immutable data record containing the latest technical indicators."""

    as_of: str
    date_range: dict[str, str]
    current_price: float
    sma_20: float
    rsi_14: float


def process_ticker(ticker: str) -> TechnicalsSnapshot:
    """
    Retrieves price data (handling cache/fetch internally),
    calculates indicators, and returns a typed snapshot.
    """
    # 1. Request data (fetch_prices.py handles the cache vs fetch logic)
    raw_data = get_prices(ticker)

    # 2. Transform into a Pandas DataFrame
    df = pd.DataFrame.from_dict(raw_data, orient="index")
    df.sort_index(inplace=True)

    closes = df["close"]

    # 3. Calculate indicators
    sma_20_val = sma(closes, window=20).iloc[-1]
    rsi_14_val = rsi(closes, window=14).iloc[-1]
    current_price_val = closes.iloc[-1]

    return TechnicalsSnapshot(
        as_of=str(df.index[-1]),
        date_range={
            "start": str(df.index[0]),
            "end": str(df.index[-1]),
        },
        current_price=float(current_price_val),
        sma_20=float(sma_20_val),
        rsi_14=float(rsi_14_val),
    )


def get_technicals_as_of(
    ticker: str,
    as_of_date: str,
    sma_window: int = 20,
    rsi_window: int = 14,
    price_period: str = "5y",
) -> TechnicalsSnapshot | None:
    """Point-in-time technicals snapshot computed from prices up to and including `as_of_date`.

    Returns None if insufficient history (need at least sma_window data points
    available on or before as_of_date). price_period defaults to "5y" so older
    backtest dates have trailing history; a 1y window starves early decision dates.
    """
    raw_data = get_prices(ticker, price_period)
    df = pd.DataFrame.from_dict(raw_data, orient="index")
    df.sort_index(inplace=True)

    # Filter to dates <= as_of_date (point-in-time discipline)
    df = df.loc[df.index <= as_of_date]
    if len(df) < sma_window:
        return None

    closes = df["close"]
    sma_val = sma(closes, window=sma_window).dropna()
    rsi_val = rsi(closes, window=rsi_window).dropna()

    if sma_val.empty or rsi_val.empty:
        return None

    return TechnicalsSnapshot(
        as_of=str(df.index[-1]),
        date_range={
            "start": str(df.index[0]),
            "end": str(df.index[-1]),
        },
        current_price=float(closes.iloc[-1]),
        sma_20=float(sma_val.iloc[-1]),
        rsi_14=float(rsi_val.iloc[-1]),
    )
