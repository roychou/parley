import pandas as pd

def sma(prices: list[float] | pd.Series, window: int) -> pd.Series:
    if isinstance(prices, list):
        prices = pd.Series(prices, dtype=float)
    return prices.rolling(window=window).mean()

"""
The relative strength index (RSI) is a momentum indicator used in technical analysis. RSI measures the speed and magnitude of a security's recent price changes to detect overbought or oversold conditions in the price of that security. The RSI is displayed as an oscillator (a line graph) on a scale of 0 to 100.
"""
def rsi(prices: list[float] | pd.Series, window: int = 14) -> pd.Series:
    """
    Calculates the RSI for a given price series.
    """
    # 1. Get the difference in price from the previous day
    delta = prices.diff()

    # 2. Separate gains (positive) and losses (negative)
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # 3. Calculate the exponential moving average of gains and losses
    # Wilder's smoothing uses alpha = 1 / window
    """
    This line will return NaN (Not a Number) for the first n-1 days of your data. It forces the script to wait until it has a full "window" of data before showing a result.
    """
    avg_gain = gain.ewm(alpha=1/window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/window, min_periods=window, adjust=False).mean()

    # 4. Calculate RS and RSI
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi
