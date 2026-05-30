"""
Backtest-mode supervisor.

A point-in-time supervisor that bypasses the MCP layer entirely. For each
(ticker, as_of) request:
  1. Fetch point-in-time fundamentals + technicals from the data layer.
  2. Call both specialists in parallel — each in a single LLM turn with data
     injected directly into the user message and `submit_analysis` forced
     via tool_choice. No agent loop, no tool dispatch, no MCP subprocess.
  3. Synthesize via the existing `synthesize` function.

Why this fork from the live MCP-based supervisor:
The MCP layer's value is specialist *agency* in data fetching — the specialist
autonomously decides what to pull and when. In a backtest, the data set is
known and point-in-time-filtered up front; MCP's agency doesn't translate.
Keeping the live path on MCP and the backtest path on direct injection lets
both serve their respective use cases without coupling.

The specialist system prompts (FUNDAMENTALS_ROLE_PROMPT, TECHNICALS_ROLE_PROMPT)
are reused verbatim. The user message includes an explicit override that the
data has already been fetched and the model should proceed directly to
submit_analysis.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Callable

from anthropic import AsyncAnthropic

from src.agents.fundamentals_specialist import FUNDAMENTALS_ROLE_PROMPT
from src.agents.scaffold import ScaffoldConfig
from src.agents.sentiment_specialist import (
    FilingSummaryCache,
    current_filing_key,
    run_sentiment_specialist,
)
from src.agents.technicals_specialist import TECHNICALS_ROLE_PROMPT
from src.backtest.cache import SignalCache, cached_signal
from src.data.fundamentals import ValuationSnapshot, get_fundamentals_as_of, pe_band
from src.data.technicals import TechnicalsSnapshot, get_technicals_as_of
from src.llm import MessageCreator
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis
from src.schemas.sentiment import SentimentAnalysis
from src.schemas.technicals import TechnicalsAnalysis
from src.synthesis import synthesize

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024


# ==========================================
# DATA LOADERS (injectable for tests)
# ==========================================


FundamentalsLoader = Callable[[str, str], ValuationSnapshot | None]
TechnicalsLoader = Callable[[str, str], TechnicalsSnapshot | None]


# ==========================================
# THE BACKTEST SUPERVISOR
# ==========================================


async def run_backtest_supervisor(
    client: AsyncAnthropic,
    ticker: str,
    as_of: str,
    fundamentals_loader: FundamentalsLoader = get_fundamentals_as_of,
    technicals_loader: TechnicalsLoader = get_technicals_as_of,
    signal_cache: SignalCache | None = None,
    include_sentiment: bool = False,
    messages_api: MessageCreator | None = None,
    scaffold_config: ScaffoldConfig | None = None,
    summary_cache: FilingSummaryCache | None = None,
) -> Decision:
    """Produce a Decision for (ticker, as_of) using point-in-time data and the real LLM.

    Loaders default to the production data layer functions. Tests can inject
    stubs to avoid touching FMP.

    signal_cache (optional) caches each specialist analysis independently:
    - fundamentals key = (filing_date, pe_band) — reused across decision dates
      until a new filing lands or P/E crosses a threshold band.
    - technicals key = as_of — they change every trading day.
    Synthesis is always recomputed from the (cached) signals, so it stays fresh.

    messages_api is the injected LLM seam, defaulting to client.messages. Pass a
    BatchLLM to coalesce all specialist calls into Batch API jobs (no per-minute
    throttle at index scale); scaffold_config then sets a high map concurrency so
    the sentiment fan-out coalesces into one wave rather than the live semaphore.
    """
    # The injected seam wins; otherwise the live client.messages. (Tests patch the
    # specialist calls and pass client=None, so resolve lazily rather than eagerly.)
    mc = messages_api if messages_api is not None else (client.messages if client else None)
    fundamentals_data = fundamentals_loader(ticker, as_of)
    technicals_data = technicals_loader(ticker, as_of)

    if fundamentals_data is None:
        raise ValueError(f"No fundamentals data available for {ticker} as of {as_of}")
    if technicals_data is None:
        raise ValueError(f"No technicals data available for {ticker} as of {as_of}")

    fundamentals_key = f"{fundamentals_data.report_date}_pe-{pe_band(fundamentals_data.pe_ratio)}"

    coros = [
        cached_signal(
            signal_cache, "fundamentals", ticker, fundamentals_key, FundamentalsAnalysis,
            lambda: _call_fundamentals_with_data(mc, ticker, as_of, fundamentals_data),
        ),
        cached_signal(
            signal_cache, "technicals", ticker, as_of, TechnicalsAnalysis,
            lambda: _call_technicals_with_data(mc, ticker, as_of, technicals_data),
        ),
    ]
    # Sentiment (optional): keyed by the current filing accession, so it's reused
    # across decision dates until a new filing. Skipped when no filing is available
    # as of the date (e.g. delisted names with no current CIK) — its absence just
    # drops one vote from synthesis.
    if include_sentiment:
        sentiment_key = current_filing_key(ticker, as_of)
        if sentiment_key:
            coros.append(cached_signal(
                signal_cache, "sentiment", ticker, sentiment_key, SentimentAnalysis,
                lambda: run_sentiment_specialist(
                    mc, ticker, as_of, config=scaffold_config, summary_cache=summary_cache
                ),
            ))

    signals = list(await asyncio.gather(*coros))
    return synthesize(ticker=ticker, signals=signals, as_of=as_of)


# ==========================================
# SPECIALIST CALLS (data-injected, single-turn)
# ==========================================


async def _call_fundamentals_with_data(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    data: ValuationSnapshot,
) -> FundamentalsAnalysis:
    data_dict = asdict(data)
    user_prompt = (
        f"Analyze {ticker} as of {as_of}.\n\n"
        "The fundamentals data has already been fetched and is provided below. "
        "Skip the get_fundamentals step in the workflow and proceed directly to "
        "submit_analysis using submit_analysis.\n\n"
        f"FUNDAMENTALS DATA:\n{json.dumps(data_dict, indent=2)}"
    )

    response = await messages_api.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=FUNDAMENTALS_ROLE_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[{
            "name": "submit_analysis",
            "description": "Submit your final FundamentalsAnalysis for the ticker.",
            "input_schema": FundamentalsAnalysis.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "submit_analysis"},
    )

    logger.info(
        f"api_usage call_site=backtest_fundamentals input_tokens={response.usage.input_tokens} "
        f"output_tokens={response.usage.output_tokens} model={MODEL}"
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            return FundamentalsAnalysis(**block.input)

    raise RuntimeError("Fundamentals specialist did not return a submit_analysis tool_use block")


async def _call_technicals_with_data(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    data: TechnicalsSnapshot,
) -> TechnicalsAnalysis:
    data_dict = asdict(data)
    user_prompt = (
        f"Analyze {ticker} as of {as_of}.\n\n"
        "The technicals data has already been fetched and is provided below. "
        "Skip the get_technicals step in the workflow and proceed directly to "
        "submit_analysis using submit_analysis.\n\n"
        f"TECHNICALS DATA:\n{json.dumps(data_dict, indent=2)}"
    )

    response = await messages_api.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=TECHNICALS_ROLE_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[{
            "name": "submit_analysis",
            "description": "Submit your final TechnicalsAnalysis for the ticker.",
            "input_schema": TechnicalsAnalysis.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "submit_analysis"},
    )

    logger.info(
        f"api_usage call_site=backtest_technicals input_tokens={response.usage.input_tokens} "
        f"output_tokens={response.usage.output_tokens} model={MODEL}"
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_analysis":
            return TechnicalsAnalysis(**block.input)

    raise RuntimeError("Technicals specialist did not return a submit_analysis tool_use block")
