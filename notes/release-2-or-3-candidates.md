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

## Backtest enhancements (Release 2)

These are explicitly listed as Release 1 limitations in `notes/backtest-design.md` and should be addressed when Release 2 expands the backtest.

- **Transaction cost modeling** — add round-trip cost assumption (e.g., 10 bps per round-trip on US large-caps) to all strategy P&L calculations.
- **Dividend reinvestment** — FMP exposes dividend data; reinvest at the ex-date close.
- **Survivorship bias** — extend the universe to include delisted tickers from the backtest window.
- **Confidence calibration** — use Release 1 backtest results to recalibrate the confidence-to-position-size mapping. The current mapping is mechanical; the data exists post-Release-1 to make it empirical.
- **LLM-driven synthesis** — replace `synthesize()` in `src/synthesis.py` with an LLM supervisor. The synthesis grounding eval is designed to survive this transition.
