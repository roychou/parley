# Release 2 / 3 Candidates

Items that don't belong in Release 1 scope but should not be lost. Each entry has a rationale and a migration path.

---

## Data layer

### IBKR as execution venue (Release 2+ — only when live trading enters scope)

**Status:** moved out of the data-migration backlog. FMP is now the single data source for both fundamentals and prices (landed Day 37). IBKR is no longer planned as a data swap.

**When IBKR enters scope:** when live execution enters scope. At that point IBKR becomes an *execution* venue (place orders through their API), not a data source. FMP continues to provide decision-time data. The flow becomes: FMP → specialist → Decision → IBKR order placement. Reconciliation between FMP-decision-price and IBKR-fill-price is logged and monitored; for a multi-week-hold fundamentals strategy on liquid US equities, drift is typically <0.5% and within acceptable bounds.

**What needs to be built when live trading enters scope:**
- IBKR Client Portal API integration (REST, no TWS required) for order placement
- Order management: position open/close, fills, partials, errors
- Reconciliation logging: FMP-decision-price vs. IBKR-fill-price per trade
- Risk controls: max position size, max daily loss, pre-trade validation

---

## Fundamentals specialist

### Sector-relative thresholds

**Why:** The current fundamentals specialist prompt uses absolute thresholds (P/E < 40, margin > 20%, revenue growth > 15%, D/E < 2). These are not sector-agnostic in practice. They produce systematic misfires:
- Banks, REITs, utilities: D/E routinely > 2 → falsely flagged as concerning
- Retail, automotive, airlines: margins routinely < 10% → falsely flagged as weak
- Mature consumer staples: revenue growth routinely 2–4% → falsely flagged as weak or bearish

**What the industry actually uses:** P/E and margin relative to sector median and historical range. Revenue growth relative to sector growth rate or company's own guidance trajectory. D/E norms are sector-specific (banks: D/A is the right metric; REITs: leverage is the business model).

**Minimum fix for Release 2:** Add `sector` field to the fundamentals MCP tool result (FMP provides this natively via the company-profile endpoint). Pass sector to the specialist prompt. Replace absolute thresholds with sector-conditional rules for the most egregious cases (financial services, real estate, utilities).

**Larger fix for Release 3:** Pass in sector median P/E, median margin, and median revenue growth alongside the company's metrics. The specialist can then reason relative to peers, not relative to universal benchmarks.

**Eval implication:** `ConsistencyEval` thresholds in `evals/fundamentals/consistency.py` have the same problem. They will produce false failures on healthy companies in low-margin or high-leverage sectors. When sector context is added to the specialist, the consistency eval thresholds must be updated to match.

---

## Universe construction (Release 2) — see `notes/universe-design.md`

Today `universe` is a static `list[str]` that conflates three jobs (eligibility,
candidate selection, analysis) into "15 curated names" — which bakes Roy's
stock-picking into the result and is survivorship-contaminated for backtests.

**Decision:** eligibility = **point-in-time S&P 500 membership**, sourced from a free
historical-constituents CSV (FMP's constituent endpoints are 402/restricted on the
current tier; verified 2026-05-29). Live mode needs only the *current* list (free) —
survivorship only affects backtests. Universe becomes a `universe_loader(date)`,
mirroring the price/fundamentals loaders.

**Candidate screen (stage ②) = event-driven selection (decided).** A name becomes a
candidate when it has a **fresh quarterly filing** that period — a trigger, not a
ranker, so it imposes no style lean and rotates attention across the whole index over
the reporting cycle (~38/week, self-pacing). Avoids double-counting the specialists,
is direction-neutral, and is the natural feed for the sentiment specialist. Held names
are always re-analyzed (`decision universe = holdings ∪ candidates`). ~500 tickers of
data hits the 250/day FMP cap (S&P 100 is a pragmatic middle).

**Prerequisite — quarterly-filings upgrade (committed):** move fundamentals from annual
to FMP `period=quarter`. Gives 4× event cadence and fixes the "up to 15 months stale"
limitation. Touches `calc_growth_yoy` (same-quarter-prior-year YoY), the fundamentals
prompt, and needs eval re-calibration.

**Deferred to the sentiment specialist:** a price-move trigger ("market moved on news")
belongs in sentiment, not the screen.

Full rationale, data caveats, and integration plan in `notes/universe-design.md`.

## Backtest enhancements (Release 2)

These are explicitly listed as Release 1 limitations in `notes/backtest-design.md` and should be addressed when Release 2 expands the backtest.

- **Transaction cost modeling** — add round-trip cost assumption (e.g., 10 bps per round-trip on US large-caps) to all strategy P&L calculations.
- **Dividend reinvestment** — FMP exposes dividend data; reinvest at the ex-date close.
- **Survivorship bias** — addressed by the point-in-time S&P 500 universe above (`notes/universe-design.md`), which also lets baselines pick from the full index rather than the curated 15.
- **Confidence calibration** — use Release 1 backtest results to recalibrate the confidence-to-position-size mapping. The current mapping is mechanical; the data exists post-Release-1 to make it empirical.
- **LLM-driven synthesis** — replace `synthesize()` in `src/synthesis.py` with an LLM supervisor. The synthesis grounding eval is designed to survive this transition.
