from typing import Literal
from pydantic import BaseModel, Field
from anthropic import Anthropic
from dotenv import load_dotenv

import asyncio
import json
import sys

from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from src.agents._helpers import build_system_prompt

load_dotenv()

client = Anthropic()
MODEL = "claude-sonnet-4-6"

class GetTechnicalsInput(BaseModel):
    ticker: str = Field(description="Stock ticker symbol, e.g. 'TSLA'")

class TechnicalAnalysis(BaseModel):
    """Structured output of the technicals specialist for a single ticker."""

    ticker: str = Field(
        description="The stock ticker symbol analyzed, e.g. 'SPY' or 'AAPL'."
    )
    as_of: str = Field(
        description="The date the analysis is anchored to, in YYYY-MM-DD format. "
                    "Must match the as_of field from the tool result."
    )
    signal: Literal["BULLISH", "BEARISH", "NEUTRAL"] = Field(
        description="The directional signal derived from the technical indicators."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the signal, from 0.0 (no confidence) to 1.0 (high confidence)."
    )
    reasoning: str = Field(
        min_length=50,
        description="Plain-English explanation of how the indicators support the signal. "
                    "Reference specific indicator values from supporting_indicators."
    )
    supporting_indicators: dict[str, float] = Field(
        description="The indicator values that informed the signal, e.g. "
                    "{'sma_20': 287.43, 'rsi_14': 52.18}. Keys must match indicator names "
                    "returned by the technicals tool."
    )

TECHNICALS_ROLE_PROMPT = """
You are a technical analyst. Your task is to produce a TechnicalAnalysis for the requested ticker based on technical indicators.

Workflow:
1. Call get_technicals(ticker) to retrieve the latest indicators and date envelope.
2. Interpret the indicators using the rules below.
3. Produce a TechnicalAnalysis as your final output.

Indicator rules:
- RSI-14 above 70 indicates overbought conditions (bearish pressure).
- RSI-14 below 30 indicates oversold conditions (bullish pressure).
- Price above SMA-20 indicates bullish trend; below indicates bearish trend.
- When indicators conflict, weight the trend (SMA) over momentum (RSI) and reflect uncertainty in confidence.

Constraints:
- The as_of field in your output MUST match the as_of from the tool result. Do not invent or infer dates.
- The supporting_indicators dict MUST contain the actual values returned by the tool, keyed by indicator name.
- Reasoning should reference specific indicator values from the tool."""
# function: analyze_ticker(ticker: str) -> TechnicalAnalysis

# 1. Build the system prompt using build_system_prompt() with a technicals-analyst role
# 2. Build the user message: "Analyze {ticker} and produce a TechnicalAnalysis."
# 3. Call client.messages.create(...) with:
#    - system=<the prompt from step 1>
#    - messages=[user message from step 2]
#    - tools=[the get_technicals MCP tool]
#    - model=<your sonnet model name>
#    - max_tokens=<reasonable number>
# 4. Run the agent loop:
#    - While the response has tool_use blocks:
#      - Dispatch the tool call (call get_technicals with the ticker)
#      - Append assistant message + tool_result message to messages
#      - Call messages.create again with updated messages
#    - When the response is text-only (no more tool_use), parse the final text as a TechnicalAnalysis
# 5. Return the TechnicalAnalysis object


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TECHNICALS_SERVER_PATH = PROJECT_ROOT / "src" / "mcp_servers" / "technicals_server.py"

TOOLS = [
    {
        "name": "get_technicals",
        "description": (
            "Fetch the latest technical indicators (SMA-20 and RSI-14) for a "
            "stock ticker, along with a date envelope (as_of, date_range). "
            "Call this first before producing the analysis."
        ),
        "input_schema": GetTechnicalsInput.model_json_schema(),
    },
    {
        "name": "submit_analysis",
        "description": (
            "Submit your final TechnicalAnalysis for the ticker. Call this exactly "
            "once after you have fetched the indicators and decided on a signal."
        ),
        "input_schema": TechnicalAnalysis.model_json_schema(),
    },
]

async def call_technicals_tool(ticker: str) -> dict:
    """Spawn the technicals MCP server and call get_technicals."""
    server_params = StdioServerParameters(
        command="python",
        args=[str(TECHNICALS_SERVER_PATH)],
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_technicals", {"ticker": ticker})
            if result.isError:
                raise RuntimeError(f"Tool call failed: {result.content}")
            # Parse the JSON text block
            text = result.content[0].text
            return json.loads(text)

async def dispatch_tool(tool_name: str, tool_input: dict) -> dict:
    """Dispatch a data-fetching tool call. submit_analysis is handled in the agent loop."""
    if tool_name == "get_technicals":
        validated = GetTechnicalsInput(**tool_input)
        return await call_technicals_tool(validated.ticker)
    return {"error": f"Unknown tool: {tool_name}"}

async def analyze_ticker(ticker: str) -> TechnicalAnalysis:
    """Run an agent loop with the registered tools."""
    system = build_system_prompt(TECHNICALS_ROLE_PROMPT)

    messages = [{"role": "user", "content": f"Analyze {ticker} and produce a TechnicalAnalysis."}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=messages,
        tools=TOOLS,
    )

    print(f"Stop reason: {response.stop_reason}")
    for _ in range(10):
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_uses:
            # Exit condition: model submitted final analysis
            if tool_use.name == "submit_analysis":
                return TechnicalAnalysis(**tool_use.input)

            # Otherwise dispatch as a data-fetching tool
            print(f"Tool call: {tool_use.name}({tool_use.input})")
            result = await dispatch_tool(tool_use.name, tool_use.input)
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
            system=system,
            messages=messages,
            tools=TOOLS,
        )

    raise RuntimeError(f"Agent did not submit analysis within 10 turns")


if __name__ == "__main__":
    result = asyncio.run(analyze_ticker("TSLA"))
    print(result)