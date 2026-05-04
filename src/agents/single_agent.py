"""First agent: single Anthropic SDK call with one Pydantic-typed tool."""

import json
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

client = Anthropic()
MODEL = "claude-sonnet-4-6"


class GetPriceHistoryInput(BaseModel):
    """Input schema for the get_price_history tool."""

    ticker: str = Field(description="The stock ticker symbol, e.g. 'SPY' or 'AAPL'")
    period: str = Field(
        default="1mo",
        description="Time period: '1mo', '3mo', '6mo', '1y', '2y', '5y'",
    )


def get_price_history(ticker: str, period: str = "1mo") -> dict:
    """Load cached price history from disk. Returns OHLCV dict keyed by date."""
    cache_dir = Path("data/cache")
    matches = sorted(cache_dir.glob(f"{ticker}_*.json"), reverse=True)
    if not matches:
        return {"error": f"No cached data for {ticker}. Run fetch_prices.py first."}
    with matches[0].open() as f:
        return json.load(f)


TOOLS = [
    {
        "name": "get_price_history",
        "description": "Fetch historical OHLCV price data for a stock ticker.",
        "input_schema": GetPriceHistoryInput.model_json_schema(),
    }
]


def run_agent(user_message: str) -> str:
    """Run a single-turn agent loop with one tool available."""
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
            validated = GetPriceHistoryInput(**tool_use.input)
            result = get_price_history(validated.ticker, validated.period)
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
    question = "What was SPY's closing price on the most recent trading day, and how does it compare to the start of the period?"
    answer = run_agent(question)
    print("\n--- Final answer ---")
    print(answer)


if __name__ == "__main__":
    main()
