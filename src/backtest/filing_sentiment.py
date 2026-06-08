"""
Filing-sentiment SENSOR — a cheap, standalone "mood reader" for the IC probe.

Purpose (forward-validation-design.md / "shorten the forward clock"): answer one question
as cheaply as possible — does a sentiment read of SEC filings carry cross-sectional
ranking skill (IC) that the fundamentals+technicals signal lacks? If even a faithful-ish
cheap proxy shows no IC, the expensive multi-agent specialist almost certainly doesn't add
ranking skill either, and we stop. Cheap-first, escalate-on-signal.

DESIGN (a triage proxy, deliberately NOT the deployed specialist — see the two "open calls"
discussion):
- DIFF-ONLY input: feed the model only the sentences that CHANGED vs the prior same-form
  filing. That's where the "what changed" signal concentrates; it cuts tokens ~80% and
  sharpens the read. Cost: it drops the steady-state-tone half of the real specialist, so a
  NULL here is trustworthy (disqualifier) but a POSITIVE would need a whole-section confirm.
- ONE Haiku call per filing (no Sonnet, no map-reduce tree) -> a single tone score in
  [-1, 1]. ~15x cheaper than the deployed scaffold.
- Cached by accession: a filing's sentiment is a pure function of its (immutable) text, so
  each unique filing is scored ONCE and reused across every decision date and future re-run.
  The cost ceiling is "# unique filings in the clean window" (~tens), not names x dates.

Reuses the exact EDGAR fetch/extract path the real specialist uses, so the text the sensor
reads matches the deployed signal even though the reasoning over it is slimmed down.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from pathlib import Path

from anthropic import AsyncAnthropic

from src.agents.safety import UNTRUSTED_PREAMBLE, wrap_untrusted
from src.agents.sentiment_specialist import FilingSummaryCache, _select_filings
from src.data.edgar import EdgarError
from src.data.edgar_filings import clean_text, extract_sections, fetch_filing_document
from src.llm import MessageCreator
from src.models import LEAF, ROOT
from src.schemas.sentiment import SentimentAnalysis

logger = logging.getLogger(__name__)

SENSOR_VERSION = "v1"  # bump on prompt / diff-logic changes -> old cache falls out of scope
SENTIMENT_CACHE_DIR = Path("data/cache/filing_sentiment")
_MAX_DIFF_CHARS = 6000  # bound the Haiku input; changed-sentences rarely exceed this

_TONE_SYSTEM = (
    "You are a terse equity sentiment sensor. You are given the sentences that CHANGED in a "
    "company's latest SEC filing (MD&A + Risk Factors) versus its prior same-type filing. "
    "Judge ONLY whether these changes read better or worse for the business outlook: raised "
    "guidance, improving demand/margins, or dropped risks are POSITIVE; lowered guidance, "
    "softening demand, margin pressure, or new/intensified risks are NEGATIVE. Submit a tone "
    "score in [-1, 1] (negative=bearish, 0=neutral/immaterial, positive=bullish) via the tool."
)

_TONE_TOOL = {
    "name": "submit_tone",
    "description": "Submit the sentiment tone score for the filing changes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tone": {"type": "number", "minimum": -1, "maximum": 1,
                     "description": "Bearish(-1) .. neutral(0) .. bullish(+1)."},
            "rationale": {"type": "string", "description": "One sentence, citing the change."},
        },
        "required": ["tone", "rationale"],
    },
}


class SentimentScoreCache:
    """Disk cache of tone scores, keyed by current-filing accession (+ sensor version).
    A filing's diff-vs-prior and tone are pure functions of immutable text, so a hit is
    valid forever. None-safe (pass None to disable, e.g. in tests)."""

    def __init__(self, root_dir: Path | str = SENTIMENT_CACHE_DIR, version: str = SENSOR_VERSION):
        self.root = Path(root_dir) / version

    def _path(self, accession: str) -> Path:
        return self.root / f"{accession}.json"

    def get(self, accession: str) -> float | None:
        path = self._path(accession)
        if not path.exists():
            return None
        try:
            return float(json.loads(path.read_text())["tone"])
        except Exception as e:  # noqa: BLE001 — a corrupt cache file is just a miss
            logger.warning(f"Sentiment-score cache read failed for {accession}: {e}. Miss.")
            return None

    def set(self, accession: str, tone: float, rationale: str = "") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(accession).write_text(
            json.dumps({"tone": tone, "rationale": rationale}, indent=2)
        )


def _section_text(ticker: str, filing: dict) -> str:
    """MD&A + Risk Factors for one filing (whole cleaned filing as fallback) — the same
    fetch/extract path the deployed specialist uses, so the sensor reads identical text."""
    html = fetch_filing_document(ticker, filing["accession"], filing["primary_document"])
    text = clean_text(html)
    sections = extract_sections(text, filing["form"])
    parts = [s for s in (sections.get("mdna"), sections.get("risk_factors")) if s]
    return "\n\n".join(parts) if parts else text


def _split_sentences(text: str) -> list[str]:
    """Cheap sentence split — clean_text collapses whitespace so there are no paragraph
    breaks; sentences are the finest deterministic unit available to diff on."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


def _changed_text(current_text: str, prior_text: str | None) -> str:
    """Sentences present in current but not prior (added or substantially rewritten),
    via a deterministic diff. With no prior filing, fall back to the current text head
    (judge on current tone alone — keep it bounded)."""
    if not prior_text:
        return current_text[:_MAX_DIFF_CHARS]
    cur = _split_sentences(current_text)
    pri = _split_sentences(prior_text)
    matcher = difflib.SequenceMatcher(a=pri, b=cur, autojunk=False)
    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(cur[j1:j2])
    changed = " ".join(added)
    return changed[:_MAX_DIFF_CHARS]


async def filing_sentiment_score(
    client: AsyncAnthropic,
    ticker: str,
    as_of: str,
    *,
    cache: SentimentScoreCache | None = None,
    messages_api: MessageCreator | None = None,
) -> float | None:
    """Tone score in [-1, 1] for the filing in force as of `as_of`, or None if no filing
    (or the ticker doesn't resolve / EDGAR errors). One Haiku call, cached by accession."""
    try:
        current, prior = _select_filings(ticker, as_of)
    except EdgarError:
        return None
    if current is None:
        return None

    if cache is not None:
        hit = cache.get(current["accession"])
        if hit is not None:
            return hit

    try:
        current_text = _section_text(ticker, current)
        prior_text = _section_text(ticker, prior) if prior else None
    except Exception as e:  # noqa: BLE001 — a fetch/parse miss drops this name from IC
        logger.info(f"{as_of} {ticker}: filing fetch failed ({e}) — skipped.")
        return None

    changed = _changed_text(current_text, prior_text)
    if not changed.strip():
        tone = 0.0  # no material change -> neutral (a real, rankable read, not missing)
        if cache is not None:
            cache.set(current["accession"], tone, "no material change vs prior filing")
        return tone

    mc = messages_api if messages_api is not None else client.messages
    resp = await mc.create(
        model=LEAF.id,
        max_tokens=256,
        system=_TONE_SYSTEM,
        messages=[{"role": "user", "content": wrap_untrusted(
            f"Ticker: {ticker}\nChanged filing language ({current['form']} "
            f"filed {current['filed']}):\n\n{UNTRUSTED_PREAMBLE}{changed}"
        )}],
        tools=[_TONE_TOOL],
        tool_choice={"type": "tool", "name": "submit_tone"},
    )
    logger.info(
        f"api_usage call_site=filing_sentiment_sensor model={LEAF.id} "
        f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}"
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_tone":
            tone = max(-1.0, min(1.0, float(block.input["tone"])))
            if cache is not None:
                cache.set(current["accession"], tone, str(block.input.get("rationale", "")))
            return tone
    return None


async def score_universe_sentiment(
    client: AsyncAnthropic,
    as_of: str,
    tickers: list[str],
    *,
    cache: SentimentScoreCache | None = None,
    max_concurrency: int = 8,
) -> dict[str, float]:
    """Tone score per name as of `as_of`. Names with no filing / fetch failure are absent
    (dropped from the IC correlation, not scored 0)."""
    sem = asyncio.Semaphore(max_concurrency)

    async def one(ticker: str) -> tuple[str, float | None]:
        async with sem:
            return ticker, await filing_sentiment_score(client, ticker, as_of, cache=cache)

    pairs = await asyncio.gather(*(one(t) for t in tickers))
    return {t: s for t, s in pairs if s is not None}


# ==========================================
# HYBRID SENTIMENT — carried tone memo + diff (middle ground: diff-only <-> full)
# ==========================================
# The full specialist re-summarizes both filings every quarter (expensive); diff-only loses
# standing tone. The hybrid carries a compact TONE MEMO forward per company: first encounter
# does a full read to seed the memo; each later filing feeds (carried memo + this quarter's
# diff) into ONE call that emits a full SentimentAnalysis AND an updated memo for next quarter.
# Cost ≈ 1 call/filing + one bootstrap read per company, while keeping absolute tone.
#
# Point-in-time clean: the memo at date T encodes only filings <= T (built chronologically;
# the runner must process dates oldest-first). Produces the SAME SentimentAnalysis schema the
# full specialist does, so synthesis consumes it identically. Live bot is untouched — this is
# a separate path the backtest opts into.

HYBRID_TONE_DIR = Path("data/cache/hybrid_tone")
HYBRID_TONE_VERSION = "hybrid-v1"  # bump on prompt/diff changes -> old memos fall out of scope

_HYBRID_SYSTEM = """You are the sentiment specialist in a multi-agent equity system, running \
in CARRY-FORWARD mode. You receive (1) a STANDING TONE memo distilled from the company's prior \
filings, and (2) WHAT CHANGED in the current filing's narrative (MD&A + Risk Factors) vs the \
prior. On the first encounter there is no standing tone and you get the current narrative in \
full to establish a baseline.

Judge the company's CURRENT sentiment by updating the standing tone with the change:
- A shift toward confidence/improving demand/raised guidance/dropped risks is BULLISH; toward \
caution/softening demand/margin pressure/new or intensified risks is BEARISH; immaterial = NEUTRAL.
- confidence (0-1) reflects how clear and material the signal is.
- reasoning (>=50 chars) must cite specific themes/changes.
- tone: a 1-2 sentence distilled STANDING TONE memo that will be carried into the NEXT filing \
(absorb the change into it — this becomes the next quarter's standing tone).
- key_themes: the current salient themes. notable_changes: concrete shifts vs prior (empty if none).
- Echo source_form and filed from the provided metadata.
Submit via the submit_sentiment tool."""


def _carried_memo(analysis: SentimentAnalysis) -> str:
    """Distill a SentimentAnalysis into the compact tone memo carried to next quarter."""
    themes = "; ".join(analysis.key_themes[:6])
    return f"[{analysis.signal}@{analysis.confidence:.2f}] {analysis.tone or ''} Themes: {themes}".strip()


async def run_sentiment_hybrid(
    messages_api: MessageCreator,
    ticker: str,
    as_of: str,
    *,
    tone_cache: FilingSummaryCache | None = None,
    synthesis_model: str | None = None,
) -> SentimentAnalysis | None:
    """Carry-forward sentiment for (ticker, as_of), or None if no filing resolves.

    tone_cache stores the carried memo keyed by filing accession. synthesis_model overrides
    the model (set by the cutoff ladder). One LLM call per filing; cached by accession via the
    caller's SignalCache, so it's computed once per filing and reused across dates."""
    try:
        current, prior = _select_filings(ticker, as_of)
    except EdgarError:
        return None
    if current is None:
        return None

    try:
        current_text = _section_text(ticker, current)
        prior_text = _section_text(ticker, prior) if prior else None
    except Exception as e:  # noqa: BLE001 — fetch/parse miss drops sentiment this period
        logger.info(f"{as_of} {ticker}: hybrid filing fetch failed ({e}) — skipped.")
        return None

    carried = tone_cache.get(prior["accession"]) if (tone_cache and prior) else None
    if carried is not None and prior_text is not None:
        changed = _changed_text(current_text, prior_text)
        body = (
            f"STANDING TONE (carried from prior {prior['form']} filed {prior['filed']}):\n{carried}\n\n"
            f"WHAT CHANGED IN THE CURRENT {current['form']} (filed {current['filed']}):\n"
            f"{changed or '(no material change vs prior filing)'}"
        )
    else:  # bootstrap: first encounter / no prior -> establish baseline from current narrative
        body = (
            f"STANDING TONE: (none on record — bootstrap baseline)\n\n"
            f"CURRENT {current['form']} NARRATIVE (filed {current['filed']}):\n"
            f"{current_text[:12000]}"
        )

    model = synthesis_model or ROOT.id
    resp = await messages_api.create(
        model=model,
        max_tokens=1024,
        system=_HYBRID_SYSTEM,
        messages=[{"role": "user", "content": wrap_untrusted(
            f"Ticker: {ticker}\nAs of: {as_of}\n{UNTRUSTED_PREAMBLE}{body}"
        )}],
        tools=[{
            "name": "submit_sentiment",
            "description": "Submit the SentimentAnalysis for the ticker.",
            "input_schema": SentimentAnalysis.model_json_schema(),
        }],
        tool_choice={"type": "tool", "name": "submit_sentiment"},
    )
    logger.info(
        f"api_usage call_site=sentiment_hybrid model={model} "
        f"input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}"
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_sentiment":
            data = dict(block.input)
            data.setdefault("ticker", ticker)
            data.setdefault("as_of", as_of)
            data.setdefault("source_form", current["form"])
            data.setdefault("filed", current["filed"])
            analysis = SentimentAnalysis(**data)
            if tone_cache is not None:  # carry the updated memo to next quarter
                tone_cache.set(current["accession"], _carried_memo(analysis))
            return analysis
    raise RuntimeError("Hybrid sentiment did not return a submit_sentiment tool_use block")


# ==========================================
# SENTIMENT-ONLY IC RUNNER (reuses ic.py + the price cache)
# ==========================================


async def run(
    dates: list[str],
    *,
    max_names: int | None,
    horizon_days: int,
    spacing_days: int,
) -> None:
    """Score the universe with the sentiment SENSOR only, then compute cross-sectional IC
    vs forward returns — the cheap read on whether sentiment alone carries ranking skill."""
    from src.backtest.ic import ICResult, cross_sectional_ic, forward_return, ic_summary
    from src.backtest.score_all import price_covered
    from src.data.fetch_prices import load_latest_cache
    from src.data.universe import nasdaq100_as_of

    client = AsyncAnthropic()
    cache = SentimentScoreCache()
    out_dir = Path("data/experiments/sentiment")
    results: list[ICResult] = []
    for as_of in dates:
        universe = [t for t in nasdaq100_as_of(as_of) if price_covered(t)]
        if max_names is not None:
            universe = universe[:max_names]
        logger.info(f"{as_of}: sentiment-scoring {len(universe)} names (Haiku, diff-only)")
        scores = await score_universe_sentiment(client, as_of, universe, cache=cache)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{as_of}.json").write_text(json.dumps(scores, indent=2, sort_keys=True))
        fwd = {t: (forward_return(load_latest_cache(t, "max"), as_of, horizon_days)
                   if load_latest_cache(t, "max") else None) for t in scores}
        res = cross_sectional_ic(as_of, scores, fwd)
        results.append(res)
        ic_str = f"{res.ic:+.3f}" if res.ic is not None else "n/a"
        print(f"{as_of}: sentiment-scored {len(scores)} names -> IC={ic_str} (n={res.n})")

    summary = ic_summary(results, horizon_days, spacing_days)
    print("\n=== SENTIMENT-ONLY IC SUMMARY ===")
    for k, v in summary.__dict__.items():
        print(f"  {k}: {v}")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Sentiment-sensor IC probe (cheap, Haiku).")
    p.add_argument("--dates", nargs="+", required=True)
    p.add_argument("--max-names", type=int, default=None)
    p.add_argument("--horizon", type=int, default=20)
    p.add_argument("--spacing", type=int, default=5)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.dates, max_names=args.max_names,
                    horizon_days=args.horizon, spacing_days=args.spacing))


if __name__ == "__main__":
    main()
