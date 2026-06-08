# Architecture

The architectural decisions behind Parley and the reasoning behind them.

## The system

Parley analyzes US equities with a multi-agent pipeline and trades them on an Interactive
Brokers **paper** account. A supervisor dispatches a ticker query to specialist agents in
parallel, receives typed Pydantic outputs, and synthesizes a BUY/HOLD/SELL decision; a risk
layer turns decisions into sized positions; an execution layer places the orders. The
supervisor never calls data sources directly — it reads specialist outputs only.

It runs in two access patterns that share prompts, schemas, and synthesis but fork at the
data/execution boundary (see "Two-regime / two-pattern split"):
- **Live / forward** — the production path. A weekly clock decides on fresh data and executes
  on the paper account. Forward paper trading is the project's *only* clean edge evaluation
  (see "Why forward, not backtest").
- **Backtest** — point-in-time replay over history, for engineering validation + baselines.

**Component inventory:**
- `src/supervisor.py` — dispatches specialists via `asyncio.gather`, then `synthesize()`
- `src/synthesis.py` — deterministic, agreement-weighted confidence vote (no LLM); explicit
  disagreement handling; BUY/SELL thresholds on the weighted score
- `src/agents/` — `fundamentals_specialist`, `technicals_specialist`, `sentiment_specialist`
  (map-reduce over EDGAR filing narrative), `news_specialist`; `scaffold.py`, `safety.py`
  (prompt-injection hardening)
- `src/mcp_servers/` — FastMCP tool surfaces for the fundamentals and technicals specialists
  (used by the standalone path; sentiment and news have no MCP server)
- `src/schemas/` — `SpecialistSignal` base + `FundamentalsAnalysis`/`TechnicalsAnalysis`/
  `NewsAnalysis`/`Decision`
- `src/data/` — `edgar.py` (PIT XBRL fundamentals + filings), `fundamentals.py`, `technicals.py`,
  `fetch_prices.py` (price cache), `universe.py` (PIT Nasdaq-100 from QQQ N-PORT), `dividends.py`,
  `sectors.py`, `filings.py`
- `src/risk.py` — inverse-vol sizing, per-name/sector/gross caps, drawdown governor/kill switch
- `src/backtest/` — `replay.py`, four baselines (`strategies.py`), `costs.py`, `metrics.py`,
  `validation.py` (walk-forward), `runlog.py`, `temporal.py` (contamination guard), `budget.py`,
  `batch.py` (Batch API), `screen.py` (event-driven candidate screen)
- `src/forward/` — `paper.py` (`PaperBook`), `decide.py`, `session.py`, `run.py` (entrypoint),
  `ibkr.py` (price + news adapters), `ibkr_execution.py` (order placement), `notify.py`
- `src/models.py` — single registry of pinned model IDs + training cutoffs + prices
- `deploy/` — Docker stack (hand-rolled IB Gateway + IBC, supercronic scheduler); `notes/deployment.md`
- `evals/` — per-specialist grounding + consistency evals

## Multi-agent over single-agent

A single-agent system is simpler to build but harder to evaluate and debug: when it gets a call
wrong, there's no clean way to localize whether the failure was in technicals, fundamentals, or
synthesis. Multi-agent costs more (N+1 model calls per decision, more orchestration, more failure
modes) but gives each component a job small enough to evaluate independently — the eval harness
measures specialists individually before synthesis can pollute a decision. Those costs are
accepted in exchange for separability.

## Direct Anthropic SDK over LangGraph

The orchestration layer is small enough that owning it directly is cheaper than the abstraction
tax — supervisor dispatch + synthesis fit in a few hundred lines. MCP is a first-class part of the
design, and the cleanest path to MCP is the Anthropic SDK + the MCP package, not a framework that
wraps both. And the LangChain ecosystem has been less stable than the underlying SDKs. The
tradeoff is more code to own, mitigated by keeping the layer small; if synthesis grows enough to
justify a framework, switching is a refactor, not a rewrite.

## MCP for specialist tool surfaces

The fundamentals and technicals specialists expose their data-access tools through an MCP server
(sentiment and news do not), for protocol-level isolation (each surface is independently
versionable/testable/runnable behind a process boundary) and reusability. The cost is protocol overhead (JSON-RPC, server lifecycle, schemas duplicated between
server and client), accepted for the isolation. *(The forward path calls the data layer directly
for point-in-time control + speed; MCP is the standalone/interactive path — see the fork below.)*

## Pydantic for structured outputs throughout

Specialist outputs are Pydantic-typed; tool input schemas derive from the models. The supervisor
receives typed objects, not free-text JSON to parse. This makes outputs comparable across runs
(the eval harness depends on it), enforces contracts at the agent boundary (synthesis depends on
it), and aids debugging. The cost is rigidity — a new field means a schema change — the right
trade for a system where reliability matters more than flexibility. Forced tool-use guarantees
schema-valid output without parsing/extraction.

## Data layer — IBKR + EDGAR, no paid vendor

Two sources, chosen so the system carries no data-vendor subscription (it briefly used FMP for
prices/foreign fundamentals; that was removed):
- **SEC EDGAR** — point-in-time XBRL fundamentals + filing narrative, by CIK (permanent), with
  true `filed` dates. Foreign private issuers (20-F/40-F, IFRS) are handled via an annual/IFRS
  fallback. `ValuationSnapshot.report_date` is the **actual filing date** (not period-end) — the
  correct availability anchor for point-in-time replay, since filings land 60–90 days after close.
- **IBKR** — daily price bars (cached to `data/cache/`) and Benzinga news, via `ib_async` against
  a running Gateway. Sectors come from a committed static map; the Nasdaq-100 universe is
  reconstructed point-in-time from QQQ's SEC N-PORT filings.

**Two-pattern split.** Fundamentals move ~quarterly (filing-keyed cache, signal reuse within a
P/E band); prices move daily (cache warmed from IBKR each run). The same split maps to the
backtest-vs-forward fork: the **backtest** calls the data layer directly with point-in-time
filters; the **forward** path warms caches from IBKR then reads them. Both share prompts,
schemas, and `synthesize()` — forked at the boundary, core preserved.

## Risk layer

A layer between synthesis and execution that owns capital preservation; it constrains, it does
not vote. Inverse-vol position sizing (confidence only tilts), a hard per-name cap, a per-sector
cap, max-gross (pro-rata scale-down, no leverage), and a drawdown governor that tapers new risk
to zero between soft (−10%) and hard (−20%) thresholds. Used identically by the backtest and the
live broker path, so a successful prompt-injection is at worst one bounded, risk-capped vote.

## Execution

`src/forward/ibkr_execution.py` is the **only** code that transmits orders. Safety model:
a paper-account guard (`_assert_paper`) requires every managed account id to start `DU` and is
re-checked immediately before any send; `transmit` defaults to False (orders previewed, not sent);
market orders only, whole shares, sized against real account equity and dropped if over-equity.
Real placement needs an explicit flag **and** Gateway's Read-Only API off. See `notes/deployment.md`.

## Why forward, not backtest (the methodology core)

When an LLM makes the decisions, a backtest over dates *inside the model's training window* is
contaminated by construction: the model has ingested what these tickers did. The leakage is in the
weights, not the inputs, so point-in-time *data* hygiene does not fix it — an in-window backtest
measures memory + strategy, confounded, and inflates exactly the multi-agent line we care about.
`src/backtest/temporal.py` operationalizes the split (clean vs. contaminated decision dates); the
honest conclusion is that **a backtest cannot establish this strategy's edge** — the deployed
model's clean post-cutoff window is too short for significance. A cross-sectional Information
Coefficient test on a model-cutoff *ladder* (an older-cutoff model manufactures a longer clean
window) sharpens this into a measured **null**: mean IC −0.005 (t −0.21) over 39 clean dates,
no detectable cross-sectional ranking edge (`results/ic_validation.md`). **Forward paper trading
is the only fully clean evaluation**, which is why it's the production path. Models are pinned (`src/models.py`) so the
forward record stays clean relative to a fixed training cutoff; a model upgrade is a deliberate
re-validation event. Full reasoning + the gating: `notes/productization.md` (GATE 0).

## Eval approach

The harness evaluates specialists independently before synthesis. **Grounding** asks: does the
reasoning accurately reflect the data given? (not whether the call is right — that needs future
prices). **LLM-as-judge with forced tool-use** for structured verdicts (`src/evals/judge.py`).
**Planted-failure tests as calibration gates** — all-pass is consistent with both "judge works"
and "judge rubber-stamps"; a planted contradiction distinguishes them, so every eval flavor ships
with one, asserting on verdict + evidence-of-detection (not the judge's internal categorization).
A greppable `api_usage call_site=… input_tokens=… output_tokens=… model=…` line at every call
site makes per-run cost aggregatable from logs.

## Scope

**Paper trading.** Real-money execution is a deliberate future step gated on the forward record
clearing GATE 0 — not out of scope forever, but consciously not yet. No web UI; no options, bonds,
crypto, or multi-asset. The narrow surface is itself architectural: every added surface dilutes the
depth of the core.

## Known gaps / what this might get wrong later

- **Synthesis is a deterministic vote**, not risk-aware or LLM-driven. A structured/LLM supervisor
  is a Phase-5 option; the synthesis eval checks Decision-vs-inputs consistency, so it survives that.
- **Sector-blind fundamentals thresholds** (absolute P/E/margin/D/E cutoffs) misfire on banks/REITs/
  utilities/low-margin retail. Sector-relative thresholds are parked (`notes/productization.md`).
- **Bank/financials fundamentals** can't be read by the revenue-concept extraction (no clean
  `Revenues` tag) — they abstain.
- **Operational depth** — reconciliation (modeled-vs-broker diff), richer fill/P&L/staleness
  monitoring, and a human-wired kill switch are still thin (Phase 2.3/2.4).

This document is updated when the gap between it and the code grows large enough to mislead.
