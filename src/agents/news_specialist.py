"""
News specialist — judgment from recent news flow (FORWARD-ONLY).

Reads the articles about a company in a trailing window as of the decision date and
emits a `NewsAnalysis` (a SpecialistSignal), so `synthesize()` consumes it exactly
like the other specialists. Large news blobs go through the map-reduce scaffold.

Why forward-only: in live/forward use, "news as of today" is genuinely point-in-time
and *after* the model's training cutoff, so it's uncontaminated. In a historical
backtest the same news is both hard to snapshot point-in-time and already in the
model's training data — so this specialist is wired into the forward path, never the
backtest supervisor. See notes/productization.md 0.0.

The **news source is injected** (NewsSource): the analysis here is vendor-agnostic and
fully testable offline; the live adapter (e.g. an Alpaca paper account's Benzinga feed,
or Finnhub/Tiingo) plugs in at that seam without touching this logic.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from src.agents.scaffold import ScaffoldConfig, analyze_text, make_llm_call
from src.llm import MessageCreator
from src.models import LEAF, ROOT
from src.schemas.news import NewsAnalysis

logger = logging.getLogger(__name__)

ROOT_MODEL = ROOT.id
LEAF_MODEL = LEAF.id
MAX_TOKENS = 1024

# (ticker, as_of, lookback_days) -> list of {title, summary, published, source?, url?}.
# Returns articles published in (as_of - lookback_days, as_of]. The live adapter must
# enforce that window so no future news leaks in.
NewsSource = Callable[[str, str, int], list[dict]]


def combine_news_sources(*sources: NewsSource) -> NewsSource:
    """Merge several news sources into one feed (e.g. IBKR/Benzinga + an RSS adapter).

    Dedupes by normalized title and returns newest-first. The merged source is what
    the specialist reads, so adding/removing a source never touches the analysis.
    Resilient: a source that errors is logged and skipped — one feed being down must
    not blind the specialist to the others. (Prefer curated financial feeds; open
    social sources carry manipulation/prompt-injection risk — see productization 3.3.)"""
    def merged(ticker: str, as_of: str, lookback_days: int) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for src in sources:
            try:
                articles = src(ticker, as_of, lookback_days)
            except Exception as e:  # noqa: BLE001 — one bad feed shouldn't kill the rest
                logger.warning(f"news source {getattr(src, '__name__', src)} failed: {e}")
                continue
            for a in articles:
                key = a.get("title", "").strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    out.append(a)
        out.sort(key=lambda a: a.get("published", ""), reverse=True)
        return out
    return merged

_MAP_SYSTEM = (
    "You are reading a batch of recent news headlines and summaries about one company. "
    "Extract concisely as bullets: concrete events/catalysts (earnings, guidance, M&A, "
    "legal/regulatory, management, product), and the tone of each. Only what's present; "
    "no speculation, no preamble."
)

_ANALYSIS_SYSTEM = (
    "Summarize this batch of recent news about one company for an equity analyst. "
    "Cover the concrete catalysts in the window, the balance of positive vs. negative "
    "coverage, and anything market-moving. Be specific and concise; no preamble."
)

_SYNTHESIS_SYSTEM = """You are the news specialist in a multi-agent equity system. From a \
summary of a company's recent news flow, produce a NewsAnalysis via the submit_news tool.

Judge the net signal from the news in the window:
- Concrete positive catalysts (beat-and-raise, favorable M&A, contract wins, upgrades) \
lean BULLISH; negative ones (misses, cut guidance, litigation, downgrades, scandal) lean \
BEARISH.
- Weigh materiality and recency, not article volume. A single material event outweighs \
many routine mentions.

Rules:
- signal is BULLISH / BEARISH / NEUTRAL; default NEUTRAL when coverage is routine, mixed, \
or immaterial.
- confidence (0-1) reflects how clear and material the net news signal is.
- reasoning must cite specific events from the window (>=50 chars).
- key_events lists the concrete catalysts (empty if none material).
- overall_tone is a short phrase; echo n_articles and lookback_days from the metadata; \
top_headlines lists a few representative headlines."""


def _format_articles(articles: list[dict]) -> str:
    parts = []
    for a in articles:
        published = a.get("published", "")
        title = a.get("title", "")
        summary = a.get("summary", "")
        parts.append(f"[{published}] {title}\n{summary}".strip())
    return "\n\n".join(parts)


async def run_news_specialist(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    news_source: NewsSource,
    *,
    lookback_days: int = 7,
    config: ScaffoldConfig | None = None,
) -> NewsAnalysis | None:
    """Produce a NewsAnalysis for (ticker, as_of) from the trailing news window, or
    None if there's no news (caller drops the news vote that period).

    news_source(ticker, as_of, lookback_days) returns the in-window articles; it must
    not return anything published after as_of (no look-ahead)."""
    articles = news_source(ticker, as_of, lookback_days)
    if not articles:
        return None
    config = config or ScaffoldConfig(root_model=ROOT_MODEL, leaf_model=LEAF_MODEL)
    llm = make_llm_call(messages_api, MAX_TOKENS)

    summary = await analyze_text(
        _format_articles(articles),
        analysis_system=_ANALYSIS_SYSTEM,
        map_system=_MAP_SYSTEM,
        llm=llm,
        config=config,
    )
    headlines = [a.get("title", "") for a in articles if a.get("title")][:5]
    return await _synthesize_news(
        messages_api, ticker, as_of, summary, len(articles), lookback_days, headlines
    )


async def _synthesize_news(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    summary: str,
    n_articles: int,
    lookback_days: int,
    headlines: list[str],
) -> NewsAnalysis:
    user_prompt = (
        f"Ticker: {ticker}\nAs of: {as_of}\n"
        f"News window: trailing {lookback_days} days, {n_articles} articles\n\n"
        f"NEWS SUMMARY:\n{summary}\n\n"
        f"REPRESENTATIVE HEADLINES:\n" + "\n".join(f"- {h}" for h in headlines)
    )
    resp = await messages_api.create(
        model=ROOT_MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYNTHESIS_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[{
            "name": "submit_news",
            "description": "Submit the NewsAnalysis for the ticker.",
            "input_schema": NewsAnalysis.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "submit_news"},
    )
    logger.info(
        f"api_usage call_site=news_synthesis model={ROOT_MODEL} "
        f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}"
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_news":
            data = dict(block.input)
            data.setdefault("ticker", ticker)
            data.setdefault("as_of", as_of)
            data.setdefault("n_articles", n_articles)
            data.setdefault("lookback_days", lookback_days)
            if not data.get("top_headlines"):
                data["top_headlines"] = headlines
            return NewsAnalysis(**data)
    raise RuntimeError("News specialist did not return a submit_news tool_use block")
