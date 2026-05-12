"""First agent: Anthropic SDK call with two Pydantic-typed tools."""

import json
from pathlib import Path

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from src.data.technicals import rsi, sma

load_dotenv()

client = Anthropic()
MODEL = "claude-sonnet-4-6"


class GetPriceHistoryInput(BaseModel):
    """Input schema for the get_price_history tool."""

    ticker: str = Field(description="The stock ticker symbol, e.g. 'SPY' or 'AAPL'")
    period: str = Field(
        default="3mo",
        description=(
            "Time period for the price history. Use '1mo' for short-term momentum "
            "questions, '6mo' or '1y' for trend analysis, '2y'+ only if explicitly "
            "asked about long-term performance. Options: '1mo', '3mo', '6mo', "
            "'1y', '2y', '5y'."
        ),
    )


def get_price_history(ticker: str, period: str = "1mo") -> dict:
    """Load cached price history from disk. Returns OHLCV dict keyed by date."""
    cache_dir = Path("data/cache")
    matches = sorted(cache_dir.glob(f"{ticker}_*.json"), reverse=True)
    if not matches:
        return {"error": f"No cached data for {ticker}. Run fetch_prices.py first."}
    with matches[0].open() as f:
        return json.load(f)


def get_technicals(ticker: str) -> dict:
    """Compute latest SMA20, SMA50, RSI14 from cached price history."""
    raw = get_price_history(ticker)
    if "error" in raw:
        return raw

    # Cache is {date_str: {"open":..., "high":..., "low":..., "close":..., "volume":...}}
    sorted_dates = sorted(raw.keys())
    closes = pd.Series(
        [raw[d]["close"] for d in sorted_dates],
        dtype=float,
    )

    sma_20 = sma(closes, window=20).dropna()
    sma_50 = sma(closes, window=50).dropna()
    rsi_14 = rsi(closes, window=14).dropna()

    return {
        "ticker": ticker,
        "as_of": sorted_dates[-1],
        "close": float(closes.iloc[-1]),
        "sma_20": float(sma_20.iloc[-1]) if len(sma_20) else None,
        "sma_50": float(sma_50.iloc[-1]) if len(sma_50) else None,
        "rsi_14": float(rsi_14.iloc[-1]) if len(rsi_14) else None,
        "n_days": len(closes),
    }


TOOLS = [
    {
        "name": "get_price_history",
        "description": "Fetch historical OHLCV price data for a stock ticker.",
        "input_schema": GetPriceHistoryInput.model_json_schema(),
    },
    {
        "name": "get_technicals",
        "description": (
            "Get the latest technical indicators (20-day SMA, 50-day SMA, RSI-14) "
            "for a stock ticker, computed from cached daily price data. Use this "
            "for questions about momentum, trend strength, or overbought/oversold "
            "conditions. Does not return historical prices — use get_price_history "
            "if you need the underlying price series."
        ),
        "input_schema": GetTechnicalsInput.model_json_schema(),
    },
]


def dispatch_tool(tool_name: str, tool_input: dict) -> dict:
    """Route a tool call to the right function."""
    if tool_name == "get_price_history":
        validated = GetPriceHistoryInput(**tool_input)
        return get_price_history(validated.ticker, validated.period)
    if tool_name == "get_technicals":
        validated = GetTechnicalsInput(**tool_input)
        return get_technicals(validated.ticker)
    return {"error": f"Unknown tool: {tool_name}"}


def run_agent(user_message: str) -> str:
    """Run an agent loop with the registered tools."""
    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=TOOLS,
        messages=messages,
    )

    print(f"Stop reason: {response.stop_reason}")

    while response.stop_reason == "tool_use":
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_uses:
            print(f"Tool call: {tool_use.name}({tool_use.input})")
            result = dispatch_tool(tool_use.name, tool_use.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result)[:4000],
                }
            )

        messages.append({"role": "user", "content": tool_results})
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        print(f"Stop reason: {response.stop_reason}")

    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n".join(text_blocks)


def main() -> None:
    question = (
        # "What's the current RSI for GOOGL, and how does the 20-day SMA "
        # "compare to the most recent close? Is the trend confirming the "
        # "momentum signal?"
        "Look at TSLA's last 30 days of closing prices and tell me whether the current RSI "
        "confirms or contradicts the price trend."
    )
    answer = run_agent(question)
    print("\n--- Final answer ---")
    print(answer)


if __name__ == "__main__":
    main()
