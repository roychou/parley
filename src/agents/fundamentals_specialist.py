"""Fundamentals Specialist Agent. Uses MCP to fetch data and Anthropic Claude to analyze it."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from anthropic import Anthropic
from anthropic.types import Message
from dotenv import load_dotenv

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.agents._helpers import build_system_prompt
from src.schemas.tools import GetFundamentalsInput
from src.schemas.fundamentals import FundamentalsAnalysis

# ==========================================
# 0. PROJECT SETUP & LOGGING
# ==========================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
MODEL = "claude-sonnet-4-6"  # Updated to standard naming, adjust if using custom endpoint
MAX_AGENT_TURNS = 10

# ==========================================
# 1. IMMUTABLE DATA MODELS
# ==========================================


# ==========================================
# 2. PROMPTS & TOOL CONFIGURATION
# ==========================================

FUNDAMENTALS_ROLE_PROMPT = """
You are a fundamentals analyst. Your task is to produce a FundamentalsAnalysis for the requested ticker based on the ticker's fundamentals.

Workflow:
1. Call get_fundamentals(ticker) to retrieve the latest fundamentals up until the as_of date.
2. Interpret the fundamentals using the rules below.
3. Produce a FundamentalsAnalysis as your final output using submit_analysis.

Fundamentals rules:
- P/E above 40 is high, P/E below 15 is low
- Profit margin above 20% is strong
- Revenue growth above 15% is strong, below 5% is weak, negative is bearish.
- D/E above 2 is concerning
- When fundamentals conflict, default to NEUTRAL and reflect uncertainty in confidence.
- If any metric is null, skip it in reasoning

Synthesis rules:
- high P/E + high revenue growth + healthy margin = justified premium
- low P/E + declining revenue + high D/E = value trap
- If signals are mixed, reasoning must enumerate which metrics point bullish vs bearish before stating overall confidence.
- strong margin + low D/E + moderate growth = healthy fundamentals, bullish
- negative or single-digit growth + margin compression = bearish regardless of P/E
- high growth + thin margin + high D/E = aggressive expansion, neutral with high uncertainty

Constraints:
- The price_date field in your output MUST match the price_date from the tool result. Do not invent or infer dates.
- The supporting_fundamentals dict MUST contain the actual values returned by the tool, keyed by indicator name.
- Data is annual filings, up to 15 months stale. Reason about company trajectory through the last reported period, not today.
- Reasoning should reference specific fundamentals values from the tool.
- Make sure to include the ticker symbol in your final output.
"""

AGENT_TOOLS = [
    {
        "name": "get_fundamentals",
        "description": "Fetch the latest fundamentals for a stock ticker. Call this first.",
        "input_schema": GetFundamentalsInput.model_json_schema(),
    },
    {
        "name": "submit_analysis",
        "description": "Submit your final FundamentalsAnalysis for the ticker. Call this exactly once.",
        "input_schema": FundamentalsAnalysis.model_json_schema(),
    },
]

# ==========================================
# 3. I/O FUNCTIONS (Side-Effects)
# ==========================================


async def fetch_fundamentals_via_mcp(ticker: str) -> dict:
    """Spawns the fundamentals MCP server and requests data."""
    logger.info(f"Connecting to MCP Server for ticker: {ticker}")
    server_params = StdioServerParameters(
        command="uv", args=["run", "python", "-m", "src.mcp_servers.fundamentals_server"]
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_fundamentals", {"ticker": ticker})

            if result.isError:
                raise RuntimeError(f"MCP Tool call failed: {result.content}")

            text = result.content[0].text
            return json.loads(text)


async def dispatch_tool(tool_name: str, tool_input: dict) -> dict:
    """Routes LLM tool calls to the appropriate external service."""
    if tool_name == "get_fundamentals":
        validated = GetFundamentalsInput(**tool_input)
        return await fetch_fundamentals_via_mcp(validated.ticker)

    logger.warning(f"LLM hallucinated unknown tool: {tool_name}")
    return {
        "error": f"Unknown tool: {tool_name}. Please use 'get_fundamentals' or 'submit_analysis'."
    }


# ==========================================
# 4. ORCHESTRATOR (Agent Loop)
# ==========================================


async def run_fundamentals_specialist(
    ticker: str, client: Anthropic, as_of: str | None = None
) -> FundamentalsAnalysis:
    """Executes the ReAct/Tool-calling loop until the LLM submits an analysis."""
    if as_of is None:
        as_of = date.today().isoformat()  # Returns 'YYYY-MM-DD'

    system_prompt = build_system_prompt(FUNDAMENTALS_ROLE_PROMPT)
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": f"Analyze {ticker} and produce a FundamentalsAnalysis up until an as_of date.",
        }
    ]

    logger.info(f"Starting agent loop for {ticker}...")

    for turn in range(MAX_AGENT_TURNS):
        logger.debug(f"Agent Turn {turn + 1}/{MAX_AGENT_TURNS}")

        # 1. Get LLM response
        response: Message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
            tools=AGENT_TOOLS,
        )

        # 2. Add assistant's response to history
        messages.append({"role": "assistant", "content": response.content})

        # 3. Process tool calls
        tool_uses = [block for block in response.content if block.type == "tool_use"]

        # If the LLM didn't call any tools, prompt it to continue
        if not tool_uses:
            logger.warning("Agent stopped without calling tools. Nudging it to continue.")
            messages.append(
                {
                    "role": "user",
                    "content": "Please call a tool to proceed or submit your analysis.",
                }
            )
            continue

        tool_results = []
        for tool_use in tool_uses:
            # EXIT CONDITION: The model provided the final structured output
            if tool_use.name == "submit_analysis":
                logger.info("Agent submitted final analysis.")
                return FundamentalsAnalysis(**tool_use.input)

            # STANDARD TOOL ROUTING
            logger.info(f"Executing tool: {tool_use.name} with input {tool_use.input}")
            try:
                result = await dispatch_tool(tool_use.name, tool_use.input)
                # Anthropic strict formatting requires returning the tool_use_id
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result)[:4000],
                    }
                )
            except Exception as e:
                logger.error(f"Tool {tool_use.name} failed: {e}")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": f"Error executing tool: {str(e)}",
                        "is_error": True,
                    }
                )

        # 4. Pass tool results back to the LLM for the next loop
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"Agent failed to submit analysis within {MAX_AGENT_TURNS} turns.")


def main() -> None:
    # Instantiate the client at the top level
    try:
        client = Anthropic()
    except Exception as e:
        logger.error(f"Failed to initialize Anthropic client: {e}")
        sys.exit(1)

    ticker_to_analyze = "NVDA"

    try:
        result = asyncio.run(run_fundamentals_specialist(ticker_to_analyze, client))
        # Note: Added fallback for result.ticker in case the model doesn't output it
        display_ticker = getattr(result, "ticker", ticker_to_analyze)
        logger.info(
            f"\nFinal Analysis for {display_ticker}:\n"
            f"Signal: {result.signal} (Confidence: {result.confidence:.2f})\n"
            f"Reasoning: {result.reasoning}"
        )
    except Exception as e:
        logger.error(f"Execution failed: {e}")


if __name__ == "__main__":
    main()
