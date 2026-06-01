from __future__ import annotations

import logging
import traceback
from dataclasses import asdict

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from src.data.technicals import process_ticker

# This server runs as a subprocess spawned by the agent over stdio. MCP's stdio
# client launches it with a sanitized environment that does NOT inherit the
# parent's vars, so load .env here or FMP_API_KEY / EDGAR_USER_AGENT go missing.
load_dotenv()

# Set up logging so we can actually see the Python stack traces in our terminal
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] Server: %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("mcp-technicals")


@mcp.tool()
def get_technicals(ticker: str) -> dict:
    """Get technical indicators and date envelope for a given ticker."""
    try:
        # 1. Attempt the pure data pipeline
        snapshot = process_ticker(ticker)
        return asdict(snapshot)

    except Exception as e:
        # 2. Log the full traceback to stderr (this will NOT corrupt MCP's stdout protocol)
        logger.error(f"Data pipeline crashed for {ticker}: {e}\n{traceback.format_exc()}")

        # 3. Return a controlled error dict.
        # The server stays alive, and the LLM receives this payload and can reason about it.
        return {
            "error": "DataFetchFailure",
            "message": str(e),
            "details": "The technicals server encountered a Python exception during processing.",
        }


if __name__ == "__main__":
    mcp.run()
