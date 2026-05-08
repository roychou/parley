from __future__ import annotations

import sys
from pathlib import Path

# When this file is imported by `mcp dev` / `mcp run`, it's loaded by filepath
# and the repo root isn't guaranteed to be on `sys.path`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp.server.fastmcp import FastMCP

from src.data.fetch_prices import load_cached_prices
from src.data.technicals import rsi, sma

mcp = FastMCP("mcp-technicals")


@mcp.tool()
def get_technicals(ticker: str) -> dict:
    df = load_cached_prices(ticker)
    closes = df["close"]
    
    sma_20 = sma(closes, window=20).iloc[-1]
    rsi_14 = rsi(closes, window=14).iloc[-1]
    
    return {
        "as_of": str(df.index[-1].date()),
        "date_range": {
            "start": str(df.index[0].date()),
            "end": str(df.index[-1].date()),
        },
        "sma_20": float(sma_20),
        "rsi_14": float(rsi_14),
    }

if __name__ == "__main__":
    mcp.run()
