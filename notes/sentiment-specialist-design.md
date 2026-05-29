# Sentiment Specialist Design — reading EDGAR filing narrative

> Design note for the qualitative specialist (EDGAR Phase 2). It reads the
> *narrative* of a filing (MD&A, Risk Factors) — management's own words — as
> opposed to the XBRL numbers the fundamentals specialist already consumes. This
> is the differentiating capability (an LLM reading prose, where rules/factor
> models can't) and the natural feed from the event-driven screen (a fresh filing
> is the trigger). Status: design; build pending. Supersedes the RLM-scaffold
> sketch in `notes/edgar-design.md` Phase 2.

---

## The deep-dive conclusion (the honest finding)

The original instinct was a **Recursive Language Model (RLM) scaffold** to avoid
context rot on large filings. Grounding it in real filings changed the shape:

- A 10-Q strips from 7.7 MB of HTML to **~59K tokens** of text; its **MD&A section
  is ~14.5K tokens** — which **fits a single Sonnet call** after structural
  extraction. (Probed on MSFT, May 2026.)
- A 10-K is **~104K tokens**; its MD&A / Risk Factors run **15–30K tokens each** —
  large enough that a single call invites rot, but a **one-level map-reduce**
  (~9 chunks → map → reduce) handles it. Filings never get so large that a chunk
  itself needs further decomposition.
- **Item-header extraction is format-sensitive**: the 10-Q regex got 0 hits on the
  10-K (different header styling), and headers appear twice (TOC ~char 35K, body
  ~100K+) so the TOC occurrence must be skipped.

**Conclusion:** the load-bearing problem is **robust structural section extraction**,
not recursion. For SEC filing sizes, the "RLM scaffold" reduces to *structural
extraction → (single call for small sections | one-level map-reduce for large ones)*.
True recursive decomposition is not warranted by the data — building it would be
over-engineering. (Knowing where the fancy technique *isn't* needed is the senior
call here; recorded as a judgment, not a shortcut.) And because the event screen is
**quarterly-filing-driven**, ~3 of 4 filings read are 10-Qs (single-call size); only
the annual 10-K hits the map-reduce path.

---

## Architecture

```
filing document (HTML, EDGAR Archives)
  │  ① structural extraction (deterministic) — item sections, skip TOC
  ▼
sections: MD&A (+ Risk Factors)
  │  ② per section: single Sonnet call if ≤ ~12K tokens
  │                 else one-level map-reduce (Haiku map chunks → Sonnet reduce)
  ▼
per-section analysis (themes, tone, notable changes)
  │  ③ synthesize → SentimentAnalysis (BULLISH/BEARISH/NEUTRAL + confidence + reasoning)
  ▼
cached as a signal (kind="sentiment", keyed by filing) → supervisor synthesis
```

### ① Structural extraction (the real work) — empirical findings (MSFT 10-Q + 10-K)
Probing both forms confirmed extraction is the brittle, load-bearing part:
- **Bare item-number anchors + largest-span**: 2/4 correct. Mis-picked Part II's
  Item 2 for the 10-Q MD&A (10-Q repeats item numbers across Part I/II) and a
  cross-reference for the 10-K Risk Factors.
- **Title-anchored headers** (item number + section title within a small window,
  entities decoded) correctly *find* the headers, but **in-body "Item N"
  cross-references pollute the end boundary** → spans collapse. Both the start AND
  end must be title-anchored section headers.
- Headers appear in the **TOC, the body header, and inline cross-refs**; styling
  varies by filer. Across 500 filers a regex extractor will have a real failure rate.

**Resolution (fits the chosen map-reduce scaffold):** treat structural extraction as
a **best-effort cost optimizer**, not a hard dependency.
1. Try a title-anchored extractor (start = item+title header; end = the next section's
   title-anchored header — e.g. MD&A ends at "Quantitative and Qualitative
   Disclosures"; pick the start/end pair with the largest span).
2. **Validate**: extracted section must contain an expected anchor (MD&A →
   "Results of Operations"; Risk → "Risk Factors") and sit in a sane size band.
3. **On validation failure, fall back to the scaffold over the *whole* cleaned
   filing** — chunk → map (tag MD&A / Risk-Factors-relevant observations per chunk) →
   reduce. Robust by construction; just costs more LLM (bounded by per-filing cache +
   Haiku leaves). When extraction succeeds, we map-reduce only the section (cheap).

So bulletproof extraction is **not required** — the scaffold's full-filing path is the
always-works floor; extraction just narrows it when it can.

### ② Section analysis
- Token-estimate the section. ≤ ~12K → one Sonnet call. Otherwise chunk (~8–10K with
  small overlap), **map** each chunk to structured observations, **reduce** to the
  section summary. One level deep is sufficient for filing sizes.

### ③ Specialist output (schema)
`SentimentAnalysis(SpecialistSignal)` — mirrors the other specialists so
`synthesize()` consumes it unchanged:
- `signal`: BULLISH / BEARISH / NEUTRAL, `confidence`, `reasoning`
- evidence: `key_themes: list[str]`, `tone: str`, `notable_changes: list[str]`
  (guidance shifts, new/removed risk factors, QoQ narrative changes — the catalyst
  content), `source_form` + `filed` date.

---

## Caching & cost
- **Filing-keyed signal cache** (the `SignalCache` we built): `kind="sentiment"`,
  `data_version` = the filing accession/`filed` date. A given 10-Q/10-K is parsed
  **once**; every decision date referencing it hits cache. This is what makes it
  affordable in a backtest.
- Section-level intermediate cache optional (keyed by filing+section+prompt version).
- **Model tiering**: default Sonnet for correctness; switch **map** (leaf) calls to
  Haiku once cost is measured — matches the "Sonnet until burn forces a split"
  budget heuristic. The reduce/synthesis stays Sonnet (judgment).

## Point-in-time validity (the advantage over news)
The specialist reads only filings with `filed <= as_of`. A filing's text *as filed
on date D* is exactly what was knowable on D — primary-source, timestamped. This
largely sidesteps the hindsight-leakage that contaminates news-sentiment backtests
(curated articles about events whose outcomes the model knows). The model may still
recall later performance from training, but the *input* is clean PIT text.

---

## Decisions (locked)
- **Framing: BOTH** — score the current filing's tone/themes AND detect material
  changes vs the prior same-form filing (guidance shifts, new/removed risk factors,
  narrative changes). Reads two filings (current + prior). Change-detection is the
  higher-signal, more-defensible half (checkable against the prior filing).
- **Build: full conditional map-reduce scaffold** up front — extraction → (single
  call if ≤ threshold | chunked map-reduce) with Haiku-leaf / Sonnet-root tiering.
- **Sections: MD&A + Risk Factors** (both carry change signal).

## Build order
1. **Section extraction** (deterministic, the load-bearing part) — `recent_filings`
   + `fetch_filing_document` + form-aware `extract_sections`; robust across 10-Q and
   10-K; unit-tested. *(this turn)*
2. **RLM scaffold** — conditional single-call/map-reduce over an injectable LLM call,
   model-tiered; stub-tested offline.
3. **Sentiment specialist + `SentimentAnalysis` schema** — current tone + prior-filing
   change detection → signal; filing-keyed signal cache.
4. **Integration** — third specialist in `backtest_supervisor` + `synthesize()`.
- **Later**: the deferred price-move/catalyst trigger folds in here (a sentiment input).
