"""Technicals Specialist Agent. Uses MCP to fetch data and Anthropic Claude to analyze it."""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from anthropic import AsyncAnthropic
from anthropic.types import Message
from dotenv import load_dotenv

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.agents._helpers import build_system_prompt
from src.schemas.tools import GetTechnicalsInput
from src.schemas.technicals import TechnicalsAnalysis

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
MODEL = "claude-sonnet-4-6"
MAX_AGENT_TURNS = 10

# ==========================================
# 1. PROMPTS & TOOL CONFIGURATION
# ==========================================

TECHNICALS_ROLE_PROMPT = """
You are a technical analyst. Your task is to produce a TechnicalsAnalysis for the requested ticker based on technical indicators.

Workflow:
1. Call get_technicals(ticker) to retrieve the latest indicators and date envelope up until the as_of date.
2. Interpret the indicators using the rules below.
3. Produce a TechnicalsAnalysis as your final output using submit_analysis.

Indicator rules:
- RSI-14 above 70 indicates overbought conditions (bearish pressure).
- RSI-14 below 30 indicates oversold conditions (bullish pressure).
- Price above SMA-20 indicates bullish trend; below indicates bearish trend.
- When indicators conflict, weight the trend (SMA) over momentum (RSI) and reflect uncertainty in confidence.

Constraints:
- The as_of field in your output MUST match the as_of from the tool result. Do not invent or infer dates.
- The supporting_indicators dict MUST contain the actual values returned by the tool, keyed by indicator name.
- Reasoning should reference specific indicator values from the tool.
- Make sure to include the ticker symbol in your final output.
- STRICT DATA ADHERENCE: If `sma_20` or `rsi_14` are returned as `null` by the tool, DO NOT invent, calculate, or estimate them. 
- MISSING DATA PROTOCOL: If indicators are null, your reasoning MUST state "Missing technical data." You must return a NEUTRAL signal with a confidence of 0.0.
"""

AGENT_TOOLS = [
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
            "Submit your final TechnicalsAnalysis for the ticker. Call this exactly "
            "once after you have fetched the indicators and decided on a signal."
        ),
        "input_schema": TechnicalsAnalysis.model_json_schema(),
    },
]

# ==========================================
# 2. I/O FUNCTIONS (Side-Effects)
# ==========================================


async def fetch_technicals_via_mcp(ticker: str) -> dict:
    """Spawns the technicals MCP server and requests data."""
    logger.info(f"Connecting to MCP Server for ticker: {ticker}")
    server_params = StdioServerParameters(
        command="uv", args=["run", "python", "-m", "src.mcp_servers.technicals_server"]
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_technicals", {"ticker": ticker})

            if result.isError:
                raise RuntimeError(f"MCP Tool call failed: {result.content}")

            text = result.content[0].text
            return json.loads(text)


async def dispatch_tool(tool_name: str, tool_input: dict) -> dict:
    """Routes LLM tool calls to the appropriate external service."""
    if tool_name == "get_technicals":
        validated = GetTechnicalsInput(**tool_input)
        return await fetch_technicals_via_mcp(validated.ticker)

    logger.warning(f"LLM hallucinated unknown tool: {tool_name}")
    return {
        "error": f"Unknown tool: {tool_name}. Please use 'get_technicals' or 'submit_analysis'."
    }


# ==========================================
# 3. ORCHESTRATOR (Agent Loop)
# ==========================================


async def run_technicals_specialist(
    ticker: str, client: AsyncAnthropic, as_of: str | None = None
) -> TechnicalsAnalysis:
    """Executes the ReAct/Tool-calling loop until the LLM submits an analysis."""
    if as_of is None:
        as_of = date.today().isoformat()  # Returns 'YYYY-MM-DD'

    system_prompt = build_system_prompt(TECHNICALS_ROLE_PROMPT)
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": f"Analyze {ticker} and produce a TechnicalsAnalysis up until an as_of date.",
        }
    ]

    logger.info(f"Starting agent loop for {ticker}...")

    for turn in range(MAX_AGENT_TURNS):
        logger.debug(f"Agent Turn {turn + 1}/{MAX_AGENT_TURNS}")

        # 1. Get LLM response
        response: Message = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
            tools=AGENT_TOOLS,
        )
        
        logger.info(f"api_usage call_site=technicals_specialist input_tokens={response.usage.input_tokens} output_tokens={response.usage.output_tokens} model={MODEL}")

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
                return TechnicalsAnalysis(**tool_use.input)

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
        client = AsyncAnthropic()
    except Exception as e:
        logger.error(f"Failed to initialize Anthropic client: {e}")
        sys.exit(1)

    ticker_to_analyze = "TSLA"

    try:
        result = asyncio.run(run_technicals_specialist(ticker_to_analyze, client))
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
