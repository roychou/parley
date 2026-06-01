"""
Sentiment specialist — judgment from the filing *narrative* (MD&A + Risk Factors).

Reads management's own words for two things ("both" framing): the **current**
filing's tone/themes, and **material changes** vs the prior same-form filing.
Large sections go through the map-reduce scaffold (no context rot). Point-in-time:
only filings with `filed <= as_of` are read, and the filing text is what was
knowable then — sidestepping the hindsight leakage that plagues news sentiment.

Output: a `SentimentAnalysis` (a SpecialistSignal), so `synthesize()` consumes it
exactly like the fundamentals/technicals signals. See
notes/sentiment-specialist-design.md.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from src.agents.safety import UNTRUSTED_PREAMBLE, wrap_untrusted
from src.agents.scaffold import LLMCall, ScaffoldConfig, analyze_text
from src.data.edgar import EdgarError, recent_filings
from src.data.edgar_filings import clean_text, extract_sections, fetch_filing_document
from src.llm import MessageCreator
from src.models import LEAF, ROOT
from src.schemas.sentiment import SentimentAnalysis

logger = logging.getLogger(__name__)

# Bump when the filing-summary prompts (_ANALYSIS_SYSTEM/_MAP_SYSTEM), the section
# extractor, or the chunking change — old cached summaries then fall out of scope.
SUMMARY_VERSION = "v2"  # v2: prompt-injection hardening (untrusted-content wrapping)


class FilingSummaryCache:
    """Disk cache for per-filing narrative summaries, keyed by SEC accession.

    A filing's summary is a pure function of its (immutable) content and the
    summarization prompts, so it's reusable forever and across tickers. The win:
    the sentiment specialist summarizes the *prior* filing for change detection, so
    without this each filing gets summarized ~twice over its life (once as the
    current filing, once as the next quarter's prior). Caching by accession halves
    that. None-safe like SignalCache (pass None to disable, e.g. in tests)."""

    def __init__(self, root_dir: Path | str, version: str = SUMMARY_VERSION):
        self.root = Path(root_dir)
        self.version = version

    def get(self, accession: str) -> str | None:
        path = self._path(accession)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())["summary"]
        except Exception as e:  # noqa: BLE001 — a corrupt cache file is just a miss
            logger.warning(f"Filing-summary cache read failed for {accession}: {e}. Miss.")
            return None

    def set(self, accession: str, summary: str) -> None:
        path = self._path(accession)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"accession": accession, "summary": summary}))

    def _path(self, accession: str) -> Path:
        return self.root / self.version / f"{accession}.json"

ROOT_MODEL = ROOT.id
LEAF_MODEL = LEAF.id
MAX_TOKENS = 1024

# Leaf (map) prompt: cheap per-chunk extraction.
_MAP_SYSTEM = (
    "You are reading an excerpt of a company's SEC filing narrative (MD&A and/or "
    "Risk Factors). Extract, concisely as bullet points: management's tone, growth/"
    "guidance statements, margin/demand commentary, and any notable or new risk "
    "language. Only what's present in this excerpt; no preamble."
)

# Reduce / single-call prompt: produce a compact narrative summary of one filing.
_ANALYSIS_SYSTEM = (
    "Summarize this company's SEC filing narrative (MD&A + Risk Factors) for an "
    "equity analyst. Cover: overall management tone, the key business themes, "
    "guidance/outlook, and the most salient risk factors. Be concise and specific; "
    "quote short phrases where they carry the tone. No preamble."
)

# Final synthesis prompt: current tone + change-vs-prior -> a directional signal.
_SYNTHESIS_SYSTEM = """You are the sentiment specialist in a multi-agent equity system. \
From a company's most recent SEC filing narrative — and how it changed versus the prior \
same-type filing — produce a SentimentAnalysis via the submit_sentiment tool.

Judge TWO things:
1. TONE: is management's current narrative optimistic, cautious, or mixed? Confident \
guidance and improving demand lean BULLISH; hedged language, demand softness, margin \
pressure, or expanded risk disclosures lean BEARISH.
2. MATERIAL CHANGE vs the prior filing: new or intensified risk factors, lowered/raised \
guidance, or a clear tone shift are high-signal. A shift toward caution is BEARISH; a \
shift toward confidence is BULLISH. If there is no prior filing, judge on tone alone and \
keep confidence modest.

Rules:
- signal is BULLISH / BEARISH / NEUTRAL; default NEUTRAL when tone is balanced or the \
change is immaterial.
- confidence (0-1) reflects how clear and material the signal is — not how detailed the text is.
- reasoning must reference specific themes/changes from the filing (>=50 chars).
- notable_changes lists concrete shifts vs the prior filing (empty if none/no prior).
- Echo source_form and filed from the provided metadata."""


def _make_llm(messages_api: MessageCreator) -> LLMCall:
    """Adapt a MessageCreator to the scaffold's (model, system, user) -> text contract."""
    async def llm(model: str, system: str, user: str) -> str:
        resp = await messages_api.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        logger.info(
            f"api_usage call_site=sentiment_scaffold model={model} "
            f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}"
        )
        return "".join(b.text for b in resp.content if b.type == "text")
    return llm


def _select_filings(ticker: str, as_of: str) -> tuple[dict | None, dict | None]:
    """The latest filing as of `as_of` (current) and the prior same-form filing."""
    eligible = [f for f in recent_filings(ticker) if f["filed"] <= as_of]
    if not eligible:
        return None, None
    current = eligible[0]
    prior = next((f for f in eligible[1:] if f["form"] == current["form"]), None)
    return current, prior


def current_filing_key(ticker: str, as_of: str) -> str | None:
    """Accession of the filing the sentiment signal is based on — the signal-cache
    data_version. None if no filing is available as of the date, or if the ticker
    doesn't resolve to a CIK (e.g. a delisted name) — caller drops sentiment then."""
    try:
        current, _ = _select_filings(ticker, as_of)
    except EdgarError:
        return None
    return current["accession"] if current else None


async def _filing_summary(
    llm: LLMCall,
    ticker: str,
    filing: dict,
    config: ScaffoldConfig,
    summary_cache: FilingSummaryCache | None = None,
) -> str:
    """Narrative summary of one filing. Uses extracted MD&A + Risk Factors when the
    best-effort extractor succeeds; otherwise falls back to the whole cleaned filing
    (the scaffold map-reduces it). See edgar_filings.extract_sections.

    Cached by accession when summary_cache is provided — the map-reduce is the
    expensive part, and the same filing is summarized again next quarter as 'prior'."""
    accession = filing["accession"]
    if summary_cache is not None:
        hit = summary_cache.get(accession)
        if hit is not None:
            return hit
    html = fetch_filing_document(ticker, accession, filing["primary_document"])
    text = clean_text(html)
    sections = extract_sections(text, filing["form"])
    parts = [s for s in (sections.get("mdna"), sections.get("risk_factors")) if s]
    content = "\n\n".join(parts) if parts else text  # fallback: whole filing
    summary = await analyze_text(
        wrap_untrusted(content),
        analysis_system=UNTRUSTED_PREAMBLE + _ANALYSIS_SYSTEM,
        map_system=UNTRUSTED_PREAMBLE + _MAP_SYSTEM,
        llm=llm, config=config,
    )
    if summary_cache is not None:
        summary_cache.set(accession, summary)
    return summary


async def run_sentiment_specialist(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    config: ScaffoldConfig | None = None,
    summary_cache: FilingSummaryCache | None = None,
) -> SentimentAnalysis | None:
    """Produce a SentimentAnalysis for (ticker, as_of) from the filing narrative, or
    None if no filing is available as of the date (caller drops sentiment that period).

    messages_api is the injected LLM seam: client.messages on the live path, or a
    BatchLLM on the batched path (which coalesces the scaffold's map fan-out).
    summary_cache (optional) reuses per-filing summaries across quarters/tickers."""
    current, prior = _select_filings(ticker, as_of)
    if current is None:
        return None
    config = config or ScaffoldConfig(root_model=ROOT_MODEL, leaf_model=LEAF_MODEL)
    llm = _make_llm(messages_api)

    current_summary = await _filing_summary(llm, ticker, current, config, summary_cache)
    prior_summary = (
        await _filing_summary(llm, ticker, prior, config, summary_cache) if prior else None
    )

    return await _synthesize_sentiment(
        messages_api, ticker, as_of, current, current_summary, prior_summary
    )


async def _synthesize_sentiment(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    current: dict,
    current_summary: str,
    prior_summary: str | None,
) -> SentimentAnalysis:
    user_prompt = (
        f"Ticker: {ticker}\nAs of: {as_of}\n"
        f"Current filing: {current['form']} filed {current['filed']}\n\n"
        f"CURRENT FILING NARRATIVE SUMMARY:\n{current_summary}\n\n"
        f"PRIOR {current['form']} NARRATIVE SUMMARY (for change detection):\n"
        f"{prior_summary or '(no prior filing available)'}"
    )
    resp = await messages_api.create(
        model=ROOT_MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYNTHESIS_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[{
            "name": "submit_sentiment",
            "description": "Submit the SentimentAnalysis for the ticker.",
            "input_schema": SentimentAnalysis.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "submit_sentiment"},
    )
    logger.info(
        f"api_usage call_site=sentiment_synthesis model={ROOT_MODEL} "
        f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}"
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_sentiment":
            data = dict(block.input)
            data.setdefault("ticker", ticker)
            data.setdefault("as_of", as_of)
            data.setdefault("source_form", current["form"])
            data.setdefault("filed", current["filed"])
            return SentimentAnalysis(**data)
    raise RuntimeError("Sentiment specialist did not return a submit_sentiment tool_use block")
