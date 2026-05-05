"""Fetch end-of-day OHLCV data from yfinance and log to JSON."""

import json
import re
from datetime import datetime
from pathlib import Path

import yfinance as yf


def fetch_history(ticker: str, period: str = "1mo") -> dict:
    """Fetch OHLCV history for a ticker. Returns a dict keyed by date string."""
    t = yf.Ticker(ticker)
    df = t.history(period=period)
    if df.empty:
        raise ValueError(f"No data returned for {ticker}")
    records = {}
    for date, row in df.iterrows():
        records[date.strftime("%Y-%m-%d")] = {
            "open": round(row["Open"], 2),
            "high": round(row["High"], 2),
            "low": round(row["Low"], 2),
            "close": round(row["Close"], 2),
            "volume": int(row["Volume"]),
        }
    return records

def get_tickers_from_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # This regex finds anything inside square brackets [] at the start of a list item
    tickers = re.findall(r'- \[(.*?)\]', content)
    return tickers

# Example usage:
# universe_tickers = get_tickers_from_file('universe.md')
# print(universe_tickers) 
# Result: ['TSLA', 'GOOGL', 'AMZN', 'INTC', 'NVDA', 'MSFT', 'GEV', ...]

def main() -> None:
    tickers_file = Path("notes/universe.md")
    ticker_universe = get_tickers_from_file(tickers_file)
    # ticker = "SPY"
    for ticker in ticker_universe:
        data = fetch_history(ticker, period="1mo")
        out_dir = Path("data/cache")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{ticker}_{datetime.now().strftime('%Y%m%d')}.json"
        with out_path.open("w") as f:
            json.dump(data, f, indent=2)
        print(f"Wrote {len(data)} days of {ticker} data to {out_path}")
        last_date = max(data.keys())
        print(f"Latest close: {data[last_date]['close']} on {last_date}")


if __name__ == "__main__":
    main()
