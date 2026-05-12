from __future__ import annotations

import logging
import traceback
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP
from src.data.fundamentals import process_ticker

# Set up logging so we can actually see the Python stack traces in our terminal
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] Server: %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("mcp-fundamentals")


@mcp.tool()
def get_fundamentals(ticker: str) -> dict:
    """Get fundamental metrics and valuation snapshot for a given ticker."""

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
            "details": "The fundamentals server encountered a Python exception during processing.",
        }


if __name__ == "__main__":
    mcp.run()
