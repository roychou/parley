"""
Map-reduce scaffold for analyzing large text (filing sections) without context rot.

Conditional, one level deep (filing sizes never need true recursion):
- text ≤ `max_single_call_tokens`  -> single root-model call (the common 10-Q case).
- larger                           -> chunk -> map each chunk with the *leaf* model
                                      (cheap extraction) -> reduce the observations
                                      with the *root* model (judgment).

Model-tiered (Haiku leaves / Sonnet root) per the budget heuristic; the LLM call is
injected so the scaffold is fully testable offline. See
notes/sentiment-specialist-design.md.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm import MessageCreator

logger = logging.getLogger(__name__)

# (model, system_prompt, user_content) -> completion text
LLMCall = Callable[[str, str, str], Awaitable[str]]


def make_llm_call(messages_api: MessageCreator, max_tokens: int = 1024) -> LLMCall:
    """Adapt a MessageCreator (client.messages or a BatchLLM) to the scaffold's
    (model, system, user) -> text contract. Shared by the qualitative specialists."""
    async def llm(model: str, system: str, user: str) -> str:
        resp = await messages_api.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        logger.info(
            f"api_usage call_site=scaffold model={model} "
            f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}"
        )
        return "".join(b.text for b in resp.content if b.type == "text")
    return llm


@dataclass(frozen=True)
class ScaffoldConfig:
    root_model: str = "claude-sonnet-4-6"
    leaf_model: str = "claude-haiku-4-5-20251001"
    max_single_call_tokens: int = 12_000
    chunk_tokens: int = 9_000
    overlap_tokens: int = 400
    # Cap parallel leaf calls so a large filing's map step can't burst past the
    # per-minute input-token rate limit (each chunk is ~chunk_tokens of input).
    max_concurrent_chunks: int = 3


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Cheap and good enough for routing."""
    return len(text) // 4


def chunk_text(text: str, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    """Split into overlapping character windows sized in approximate tokens.

    Windows break on the nearest whitespace before the boundary so chunks don't
    split mid-word. Overlap preserves context across boundaries.
    """
    chunk_chars = chunk_tokens * 4
    overlap_chars = overlap_tokens * 4
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        if end < len(text):
            # back off to the last whitespace so we don't cut a word
            ws = text.rfind(" ", start, end)
            if ws > start:
                end = ws
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return [c for c in chunks if c]


async def analyze_text(
    text: str,
    *,
    analysis_system: str,
    map_system: str,
    llm: LLMCall,
    config: ScaffoldConfig | None = None,
) -> str:
    """Analyze `text`, transparently map-reducing it when it's too large for one call.

    - analysis_system: prompt that produces the final analysis. Applied to the raw
      text (single-call path) or to the concatenated map observations (reduce path).
    - map_system: prompt that extracts observations from one chunk (leaf step).
    """
    config = config or ScaffoldConfig()

    if estimate_tokens(text) <= config.max_single_call_tokens:
        return await llm(config.root_model, analysis_system, text)

    chunks = chunk_text(text, config.chunk_tokens, config.overlap_tokens)
    sem = asyncio.Semaphore(config.max_concurrent_chunks)

    async def _map(chunk: str) -> str:
        async with sem:
            return await llm(config.leaf_model, map_system, chunk)

    observations = await asyncio.gather(*(_map(chunk) for chunk in chunks))
    combined = "\n\n---\n\n".join(observations)
    return await llm(config.root_model, analysis_system, combined)
