# Architecture

This document captures the architectural decisions behind Parley and the reasoning behind them.

## The system

Parley is a multi-agent system for analyzing US equities. A supervisor dispatches a ticker query to specialist agents in parallel, receives typed Pydantic outputs from each, and synthesizes a BUY/HOLD/SELL recommendation. Each specialist exposes its data-access tools through an MCP server. The supervisor never calls data sources directly — it reads specialist outputs only.

Release 1 ships: supervisor, two specialists (fundamentals, technicals), deterministic synthesis, an eval harness with grounding evals for both specialists, and a backtest harness. Releases 2 and 3 expand the specialist set and replace deterministic synthesis with an LLM-driven supervisor.

**Current component inventory:**
- `src/supervisor.py` — dispatches specialists via `asyncio.gather`, then calls `synthesize()`
- `src/synthesis.py` — deterministic confidence-weighted vote; BUY/SELL thresholds at ±0.3 weighted score
- `src/agents/fundamentals_specialist.py`, `src/agents/technicals_specialist.py` — agent loops over MCP tool calls
- `src/mcp_servers/fundamentals_server.py`, `src/mcp_servers/technicals_server.py` — FastMCP servers; each exposes one tool
- `src/schemas/` — Pydantic schemas: `SpecialistSignal` base, `FundamentalsAnalysis`, `TechnicalsAnalysis`, `Decision`
- `src/evals/judge.py` — reusable LLM-as-judge wrapper; tool-use forcing for structured output
- `src/evals/base.py` — `EvalProtocol` and `EvalResult` contracts
- `evals/fundamentals/grounding.py`, `evals/technicals/grounding.py` — grounding eval per specialist
- `src/data/fundamentals.py`, `src/data/technicals.py`, `src/data/fetch_prices.py` — data pipelines

## Multi-agent over single-agent

Single-agent trading systems are simpler to build but harder to evaluate and harder to debug. When a single-agent system gets a call wrong, there is no clean way to localize whether the failure was in the technicals reasoning, the fundamentals reasoning, or the synthesis — the model did all of it in one pass.

Multi-agent costs more (more LLM calls per decision, more orchestration code, more failure modes) but produces a system where each component has a job small enough to evaluate independently. The eval harness measures specialists individually before measuring synthesis. If a specialist is consistently wrong about something, that surfaces in its eval before it pollutes the supervisor's decisions.

The cost is real. Each decision triggers N+1 model calls instead of one, latency multiplies, and the orchestration layer has to be designed and maintained. Those costs are accepted in exchange for separability.

## Direct Anthropic SDK over LangGraph

LangGraph is the obvious framework choice for multi-agent orchestration. Choosing not to use it requires explanation.

Three reasons. First, the orchestration layer is small enough that owning it directly is cheaper than the abstraction tax. Supervisor dispatch and synthesis fit in a few hundred lines of focused code; LangGraph's value proposition assumes more complexity than this system has. Second, MCP is a first-class part of the design (see below), and the cleanest path to MCP is through the Anthropic SDK and the MCP Python package, not through a framework that wraps both. Third, the LangChain ecosystem has been less stable than the underlying SDKs over the past year — committing to LangGraph for an 11-week build introduces ecosystem risk that a direct-SDK approach doesn't carry.

The tradeoff: more code to own. Mitigated by keeping the orchestration layer small and focused. If the synthesis logic grows enough to justify a framework, switching is a refactor, not a rewrite.

## MCP for specialist tool surfaces

Each specialist exposes its data-access tools through an MCP server. The fundamentals specialist's MCP server exposes tools for revenue lookup, ratio calculation, earnings history. The technicals specialist's MCP server exposes tools for price history, indicator computation, pattern checks.

This is more work than wiring tools directly into the supervisor's process via Pydantic schemas. Two reasons it's worth the work.

First, protocol-level isolation: each specialist's tool surface is independently versionable, independently testable, and independently runnable. A bug in the fundamentals tool layer cannot corrupt the technicals tool layer because they live behind separate MCP servers with separate process boundaries.

Second, reusability: a well-designed MCP server for financial fundamentals or for technical indicators is useful outside this project. Anything that needs the same data through an agent can plug into the same server.

The cost is the protocol overhead — JSON-RPC over a transport, server lifecycle management, schema definitions duplicated between server and client. Acceptable for the isolation properties.

## Pydantic for structured outputs throughout

Specialist outputs are Pydantic-typed. Tool input schemas are derived from Pydantic models via `model_json_schema()`. The supervisor receives `TechnicalAnalysis` and `FundamentalAnalysis` objects, not free-text JSON to be parsed.

Pydantic-everywhere makes specialist outputs comparable across runs (the eval harness depends on this), enforces contracts at the boundary between agents (synthesis depends on this), and makes the system more debuggable when things go wrong. The cost is rigidity — adding a new field to a specialist's output means updating its schema. The right tradeoff for a system whose reliability matters more than its flexibility.

## Data layer — two-regime split

Fundamentals data and technicals data have fundamentally different update cadences, which shapes the data layer design.

Fundamentals data (revenue, EPS, margins, P/E, debt ratios) comes from quarterly filings. The data changes four times a year. The fundamentals pipeline fetches from yfinance and caches to disk (`data/cache/fundamentals/`). Re-fetching on every run is wasteful; a cache with a TTL that aligns with reporting cadence is the right design. The specialist reads cached data unless the cache is stale.

Technicals data (price history, SMA, RSI) changes daily. The technicals pipeline fetches fresh price history from yfinance on each run and computes indicators in-process. Caching is not appropriate here — the value of the technicals signal depends on it being current.

This two-regime split — slow-moving filings vs. fast-moving price data — maps to the two MCP servers and the two data pipelines. It's why the fundamentals server and the technicals server have different data access patterns even though they look structurally identical from the outside.

## Eval approach

The eval harness evaluates specialists independently before evaluating synthesis. The principle: if a specialist's reasoning is systematically wrong about something, that should surface in its eval before it affects the supervisor's decisions.

**Grounding** is the first eval flavor for both specialists. Grounding asks: does the specialist's reasoning accurately reflect the data it was given? It does not ask whether the signal (BULLISH/BEARISH) is the right call — that requires ground truth from future price moves. Grounding is checkable now, against the data already in the system.

**LLM-as-judge with structured output.** `src/evals/judge.py` wraps a single Anthropic API call. The judge is given a system prompt (rubric), a user prompt (evidence + reasoning to evaluate), and a Pydantic schema. Tool-use forcing ensures the model returns structured output matching the schema — no parsing, no extraction logic. The eval module constructs the prompts and schema; `judge.py` is a pure API wrapper.

**Planted-hallucination tests as calibration gates.** Three all-pass results are consistent with both "the judge works correctly" and "the judge rubber-stamps." A planted contradiction — a `FundamentalsAnalysis` or `TechnicalsAnalysis` constructed to contain a known error — distinguishes them. Every new eval flavor ships with a planted-failure test. The test asserts on verdict + evidence-of-detection, not on the judge's internal categorization. Over-specifying categorization produces flaky tests when the judge has legitimate discretion in how it decomposes multi-clause reasoning.

**Token logging convention.** Every API call site logs usage in a greppable format:
```
api_usage call_site=<site> input_tokens=<n> output_tokens=<n> model=<model>
```
Call sites: the specialist agent loop, `src/evals/judge.py`. This convention is future-aggregatable — per-run cost can be derived by grepping log output without modifying any code.

## Out of scope

No real-money execution. No broker integration. No production-grade risk controls. No web UI. No options, bonds, crypto, or multi-asset support. These are out of scope across all releases.

The decision to keep the surface narrow is itself architectural. Every additional surface dilutes the depth of the core system.

## What this might get wrong later

- **Synthesis.** The current deterministic vote (confidence-weighted score, ±0.3 thresholds) is a stub. Release 2 replaces it with an LLM-driven supervisor that has discretion in weighting and rationale. That transition will need its own writeup. The eval harness is designed to survive it — the synthesis eval checks internal consistency of the Decision against specialist inputs, not against formula output, so it remains valid when the formula goes away.
- **Model selection.** `judge.py` defaults to `claude-sonnet-4-6` with a single-line swap to Haiku. If API cost forces a cheaper model for some eval types, that's a per-eval decision, not a system-wide change. Specialists currently use Sonnet; the token logging convention lets cost be derived from logs before committing to a cheaper model.
- **MCP server reusability.** The reusability claim weakens if both MCP servers turn out to be thin wrappers with no logic worth sharing. The protocol-isolation argument remains valid regardless.
- **Fundamentals cache TTL.** The cache design assumes quarterly data. If a ticker reports mid-quarter corrections or the cache grows stale in test environments, the TTL logic will need explicit handling. Not designed yet.
- **`supporting_technicals` schema and temporal claims.** The DIRECTIONAL/TEMPORAL grounding rule requires explicit lookback metadata (e.g., "20-day momentum") to evaluate trend claims. `TechnicalsAnalysis` currently has `date_range` but no per-indicator lookback window. This is not a blocking gap for current eval runs — planted-failure tests confirmed the judge correctly flags trend claims without temporal grounding. It becomes a real gap when the specialist starts making legitimate trend claims that should be grounded but aren't.

This document is updated at end-of-sprint when the gap between the doc and the code becomes large enough to mislead a reader.