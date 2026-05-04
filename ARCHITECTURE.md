# Architecture

This document captures the architectural decisions behind Parley and the reasoning behind them.

## The system

Parley is a multi-agent system for analyzing US equities. A supervisor agent receives a query about a ticker, dispatches the query to specialist agents in parallel, receives structured outputs from each specialist, and synthesizes a BUY/HOLD/SELL recommendation. Specialists are domain-focused: fundamentals analyst, technicals analyst, news analyst, risk manager. Each specialist exposes its data-access tools through an MCP server. The supervisor never calls data sources directly — it only reads specialist outputs.

Release 1 ships the supervisor plus two specialists (fundamentals, technicals) with synthesis logic and a backtest harness. Releases 2 and 3 expand the specialist set and refine the system.

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

## Out of scope

No real-money execution. No broker integration. No production-grade risk controls. No web UI. No options, bonds, crypto, or multi-asset support. These are out of scope across all releases.

The decision to keep the surface narrow is itself architectural. Every additional surface dilutes the depth of the core system.

## What this might look wrong about later

A few things will likely surface during build that this document either has wrong or hasn't anticipated:

- The synthesis logic is described above as "combines specialist outputs into a recommendation." That's hand-wavy. The actual synthesis policy — voting, weighted combination, supervisor-as-judge, something else — will be designed during Release 1 build, and the choice will probably need its own writeup.
- API costs may force a Haiku-for-specialists, Sonnet-for-supervisor split sooner than expected. If that happens, the architecture is unchanged but model selection becomes a per-component decision.
- MCP server design may turn out to be more mechanical than the rationale above suggests, in which case the protocol-isolation argument is the only real one and the reusability claim weakens.

This document will be updated as those things surface.