# EDGAR Design — point-in-time fundamentals (and filing narrative) from SEC

> Design note. Decided: source fundamentals from SEC EDGAR instead of FMP, after
> repeatedly hitting FMP free-tier walls (restricted constituents, 5-statement
> cap, rate limit). EDGAR is free, has true as-filed dates and deep history, and
> exposes the literal filing text. Status: design; phase 1 build pending. See
> `notes/universe-design.md` (it consumes quarterly filing events) and
> `notes/backtest-design.md`.

---

## Why EDGAR

- **Free, no key.** Fair-use limit ~10 req/s; must send a `User-Agent` with contact
  info (SEC blocks anonymous traffic).
- **Native point-in-time.** Every XBRL fact carries a real `filed` date, so "what was
  knowable as of date D" = facts with `filed <= D`. Restatements are handled correctly
  (pick the latest value known as-of D). This is a stronger PIT story than any vendor's
  convenience layer.
- **Deep history + quarterly.** Confirmed on MSFT: 337 diluted-EPS entries spanning
  2009→2026, quarterly and annual. This *solves the YoY problem the FMP free tier could
  not* (FMP capped at 5 statements → only the latest quarter had a same-quarter-prior-
  year partner; EDGAR has the full history, so true quarterly YoY works).
- **Literal filing text.** Beyond the numbers, the full 10-K/10-Q documents are
  available (MD&A, Risk Factors, footnotes) — the substrate for the narrative/sentiment
  specialist (see Phase 2).

**What EDGAR does NOT cover:** market prices (keep FMP-free — fine for prices: 5y,
250/day) and index constituents (free `fja05680/sp500` CSV, per universe-design). EDGAR
replaces *fundamentals* only.

---

## The two layers

1. **Structured XBRL facts** — `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`.
   One call returns *all* concepts for a company, each with value, period (`start`/`end`),
   fiscal tags (`fy`/`fp`), `form`, and `filed`. → the numbers for `ValuationSnapshot`.
2. **Filing documents** — the actual 10-Q/10-K HTML in the EDGAR Archives. → the
   narrative for Phase 2.

Supporting endpoints:
- Ticker → CIK: SEC's authoritative `https://www.sec.gov/files/company_tickers.json`
  (one file; do NOT use FMP for this).
- Submissions list: `https://data.sec.gov/submissions/CIK##########.json` — every
  filing with `form`, `filingDate`, `reportDate`, `accessionNumber`, `primaryDocument`.
  Drives both the document URLs and the event-driven screen (a fresh 10-Q = a catalyst).

---

## Phase 1 — numeric fundamentals client (the immediate build)

Replaces the FMP fundamentals source under the *existing* interface. `ValuationSnapshot`,
`get_fundamentals_as_of`, the signal cache, and the loaders stay the same — only the
data source underneath changes.

### Field → XBRL concept mapping (`ValuationSnapshot`)
EDGAR fills the filing-derived fields; price-derived ones (`price_date`, `pe_ratio`)
still come from the price layer (FMP-free prices × EDGAR `diluted_eps`).

| Field | XBRL concept(s) | Notes |
|---|---|---|
| `diluted_eps` | `EarningsPerShareDiluted` | unit `USD/shares`; pick the **quarterly** entry (see disambiguation) |
| `revenue` (for margin/growth) | `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues` → `SalesRevenueNet` | priority fallback; companies switch tags across eras (MSFT uses all three historically) |
| `net_income` | `NetIncomeLoss` | → `profit_margin = net_income / revenue` |
| `debt_to_equity` | total debt / `StockholdersEquity` | debt is the messy one — see below |
| `report_date` | the fact's `filed` | the real filing date (point-in-time anchor) |
| `period_end_date` | the fact's `end` | fiscal period close |
| `rev_growth_yoy` | revenue(q) vs revenue(same q, prior year) | deep history makes true YoY computable |

### Period disambiguation (the key XBRL gotcha)
`companyfacts` mixes **YTD and quarterly** facts for the same period. Confirmed on MSFT
Q3 FY2026: revenue appears as both a 9-month YTD ($241.8B, `start=2025-07-01`) and the
3-month quarter ($82.9B, `start=2026-01-01`); EPS as 13.14 (YTD) vs 4.27 (quarter).
- **Rule:** compute span = `end - start`; treat ~85–95 days as a true quarter. Use the
  quarterly fact, not the YTD one.
- **Q4 derivation:** Q4 is usually not filed standalone (the 10-K covers the full year),
  so for flow metrics (revenue, net income) derive `Q4 = FY − Q1 − Q2 − Q3`. Stock
  metrics (equity, debt) are point-in-time balances — take the period-end value directly.

### Debt / D-E assembly (hardest)
No single clean total-debt tag. Assemble with fallbacks, e.g.
`LongTermDebtNoncurrent + (LongTermDebtCurrent | DebtCurrent)`, or a clean tag if the
filer provides one. If unreliable for a given filer, prefer NaN over a wrong number
(the specialist already skips null metrics). Reconsider whether D/E is the right
leverage metric vs a more cleanly-tagged alternative.

### Point-in-time & restatements
To build the fundamentals known as-of `as_of`: take facts with `filed <= as_of`; for
each period keep the entry with the **latest** such `filed` (this yields what was known
then, correctly reflecting any restatement that had been filed by `as_of`). Reconstruct
a per-ticker **filings history** (one record per fiscal quarter with its metrics and
`filed` date); `get_fundamentals_as_of` then picks the latest record with `filed <= as_of`
— same selection logic as today.

### Validation: FMP-free as a dev-time cross-check oracle
For the ~5 recent annual / ~5 recent quarters FMP-free covers, write tests asserting
EDGAR-extracted revenue / EPS / debt match FMP within tolerance. A cheap calibration
gate that catches extraction bugs (revenue-tag picks, period disambiguation, debt
assembly). FMP is a **test harness only** — never a runtime dependency (it can't see the
deep history that is EDGAR's whole point).

### Mechanics
- **CIK map:** download `company_tickers.json` once into `data/reference/`.
- **Caching:** reuse the existing disk-cache pattern (one `companyfacts` per ticker;
  cache the derived filings history). One call per company → ~500 calls = minutes,
  well within the 10 req/s limit.
- **Etiquette:** `User-Agent: parley-research <contact email>`; throttle to <10 req/s.

---

## Phase 2 — narrative layer (substrate for the sentiment specialist)

The richest LLM input is management's own words. From the filing documents:
- Parse to the relevant **sections by item header** — MD&A (10-Q Item 2 / 10-K Item 7),
  **Risk Factors** (Item 1A), forward guidance, footnotes. Do NOT feed whole filings
  (the MSFT 10-Q was 7.7 MB / 100+ pages) — extract surgically, then chunk.
- **Point-in-time advantage over news:** a filing's text *as filed on date D* is exactly
  what was knowable on D — primary-source and timestamped. This largely sidesteps the
  hindsight-leakage problem that contaminates news-based sentiment backtests. (The model
  may still recall later performance from training, but the *input* is clean PIT text.)
- **Signals it enables:** management tone, guidance changes, new/removed risk factors,
  quarter-over-quarter narrative shifts — feeding a narrative/sentiment specialist and
  enriching the fundamentals specialist beyond P/E.

Phase 2 is the natural home for the deferred "price-move / catalyst" sentiment work.

### Reading large filings without context rot — the scaffold
A 7.7 MB filing stuffed into one Sonnet call triggers lost-in-the-middle / attention
dilution well before the context-window limit. Defend in layers (cheapest first):

1. **Structural extraction (deterministic, free).** Parse to the relevant item sections
   by header before any LLM call — 7.7 MB → tens of KB. This removes most rot pressure
   for zero tokens; do it first, always.
2. **Recursive scaffold over what remains.** For still-large sections (Risk Factors can
   run 30+ pages), a root controller works over a *structural map* (section list / chunk
   index) and dispatches sub-queries to spans — extract claims/tone per chunk, then
   aggregate — so no single call ingests the bloated whole. (This is the pragmatic core
   of a "Recursive Language Model" scaffold: recursive map-reduce with a controller that
   can further decompose oversized spans. Honest trade-offs: multiplies calls
   cost/latency, and adds aggregation-fidelity failure modes — reserve it for the
   large-prose case, not as a default.)

Two things that make it affordable:
- **Amortize via the signal cache.** Key the narrative signal on the **filing**
  (accession / `filed` date), not the decision date — the expensive recursive parse of a
  given 10-Q runs once per filing; every decision date that references it hits cache.
  This is exactly the per-data-version caching `SignalCache` was built for.
- **Model-tier the scaffold.** Haiku at the leaves (cheap "extract claims/tone from this
  chunk"), Sonnet at the root (synthesis/judgment) — matches the "Sonnet until burn
  forces a split" budget heuristic; the leaf work splits cleanly.

The scaffold is an *internal* detail of the narrative/sentiment specialist; its output is
cached per-filing like any other signal. Detailed scaffold design is a separate pass
(after the Phase 1 numeric build).

---

## Open items / caveats
- Revenue-tag and debt-tag coverage varies by filer; the fallback lists will need
  widening as more tickers are onboarded (validate via the FMP cross-check on a sample).
- Non-calendar fiscal years (MSFT = June) — `fy`/`fp` handle this; YoY must match `fp`,
  not calendar quarter.
- Foreign filers (20-F) and non-XBRL/older filings are out of scope (S&P 500 large-caps
  file 10-K/10-Q with XBRL; fine for our universe).
- Amended filings (10-K/A, 10-Q/A) appear as additional `filed` entries — handled by the
  PIT "latest filed <= as_of" rule.
