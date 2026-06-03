# Parley

A multi-agent system that analyzes US equities and trades them on a **paper** account.
Specialist agents (fundamentals, technicals, sentiment, news) each produce a typed analysis;
a supervisor synthesizes them into a BUY/HOLD/SELL decision; a risk layer sizes the position;
and the result is executed against an Interactive Brokers paper account. Built on the direct
Anthropic SDK with an own orchestration layer — deliberately not a framework.

> Paper trading only. Nothing here is financial advice; trading real money carries real risk
> of loss. Real-money execution is a future, deliberate step (see `notes/productization.md`).

## How it works

```
screen (recent SEC filers ∪ holdings)
  → fundamentals + technicals + sentiment + news specialists  (parallel, typed Pydantic out)
    → synthesize()  (deterministic, agreement-weighted confidence vote)
      → risk layer  (inverse-vol sizing, per-name/sector/gross caps, drawdown governor)
        → execute   (IBKR paper account, or a simulated book)
```

- **Universe:** point-in-time **Nasdaq-100**, reconstructed from QQQ's SEC N-PORT filings.
- **Data — no paid vendor:** **IBKR** (prices + Benzinga news) and **SEC EDGAR** (point-in-time
  XBRL fundamentals + filing narrative). Prices are cached; fundamentals are filing-date anchored.
- **Two execution modes:** a simulated `PaperBook`, or live orders on the IBKR paper account
  (`--execute ibkr --transmit`), guarded so it refuses any non-paper account.

## The honest methodology bit

An LLM backtest is **contaminated by construction** — the model has ingested what these tickers
did during its training window, so an in-window backtest measures memory, not edge. The only
clean evaluation is **forward paper trading** on post-cutoff, unseen data. So parley runs a live
weekly forward clock and treats that accruing record — not any backtest number — as the real
verdict. See `notes/productization.md` (GATE 0) for the full reasoning.

## Layout

- `src/agents/`, `src/supervisor.py`, `src/synthesis.py` — specialists, dispatch, synthesis
- `src/mcp_servers/` — each specialist's tool surface as an MCP server (standalone path)
- `src/data/` — EDGAR fundamentals, technicals, price cache, Nasdaq-100 universe, dividends
- `src/risk.py` — the capital-preservation layer
- `src/backtest/` — point-in-time replay, baselines, costs, metrics, walk-forward, temporal guard
- `src/forward/` — the live clock: paper book, decision provider, IBKR adapters + execution, alerts
- `deploy/` — Docker stack (hand-rolled IB Gateway + IBC, scheduler) — see `notes/deployment.md`
- `evals/` — per-specialist grounding/consistency evals; `ARCHITECTURE.md` for the rationale

## Running it

```bash
uv sync
cp .env.example .env          # ANTHROPIC_API_KEY + EDGAR_USER_AGENT (contact info)
uv run pytest                 # the deterministic suite

# one forward session (needs a running IB Gateway/TWS for live data):
uv run python -m src.forward.run --as-of "$(date +%F)" --max-llm-usd 5
```

Python 3.12, managed with `uv`. The always-on deployment (VPS + Docker) is in `notes/deployment.md`.

## License

MIT.
