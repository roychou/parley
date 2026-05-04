# Parley

Multi-agent equities trading bot. Paper trading only. Built as a portfolio project to demonstrate technical PM-with-engineering-DNA archetype: production-quality multi-agent orchestration, eval-driven product development, and MCP fluency, all without leaning on framework abstractions.

## What it does

Parley analyzes US equities and produces BUY/HOLD/SELL recommendations through a multi-agent system. A supervisor agent dispatches questions to specialist agents (fundamentals, technicals, news, risk), receives structured analyses, and synthesizes a final decision. Performance is measured by an eval harness against three baselines: random, buy-and-hold SPY, and a single-indicator strategy.

Universe: 10–15 US equities, mix of large-cap stable and mid-cap volatile.

## Architecture

Direct Anthropic SDK plus Pydantic plus an own orchestration layer. Each specialist's tool surface is implemented as an MCP server. Deliberately not LangGraph or LangChain — the orchestration design is part of the project's value, not an implementation detail to outsource.

See `ARCHITECTURE.md` for the full rationale.

## Release roadmap

- **Release 1 — June 12, 2026.** Two specialists (fundamentals, technicals) plus supervisor and synthesis. Full backtest over 6–12 months on 10–15 tickers. Per-specialist evals.
- **Release 2 — July 3, 2026.** Adds news analyst and risk manager specialists. Synthesis refactored for four specialists.
- **Release 3 — July 24, 2026.** Direction TBD at Release 2 retrospective.

## Out of scope (all releases)

Real money. Broker integration. Production-grade risk controls. Web UI. Options, bonds, crypto, multi-asset support. This is paper trading on cached data — that scope discipline is deliberate, not a limitation.

## Running it

```bash
uv sync
cp .env.example .env  # add your ANTHROPIC_API_KEY
uv run python -m src.data.fetch_prices
uv run python -m src.agents.single_agent
```

Python 3.12 required. Managed via `uv`.

## Contributing

See `CONTRIBUTING.md`. There is a deliberate AI-free rule on skill-building components — read it before sending anything.

## License

MIT.