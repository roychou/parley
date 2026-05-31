# Productization Roadmap — from evaluation harness to real-money tool

> Framing change (Sat 31 May 2026): parley is no longer scoped as an evaluation
> artifact. The goal is now a tool the author would trust with **personal real
> capital**. This document supersedes the Release-1/2/3 gating in
> `release-2-or-3-candidates.md`: gates are now **capability- and
> capital-preservation-based, not calendar-based**. Nothing here is financial
> advice; trading real money carries real risk of loss.
>
> **This is an independent track** from the product in `_plans/local-notes.md`
> (decided 31 May 2026: two separate tracks, neither subordinate). parley advances
> on the gates below, on its own time budget — not on product dates.

---

## Guiding principle

**The bottleneck is not more alpha. It is trust and downside protection.**
Adding signals to an un-validated, cost-free, risk-naive system multiplies the
ways to lose money confidently. So the order is: (1) prove the edge is real and
survives friction, (2) protect the downside, (3) execute reliably, (4) only then
enhance the signal. Each phase has an explicit **gate** — do not proceed (and
certainly do not deploy capital) until it is met.

## Honest current state

**Solid (genuine assets):**
- Point-in-time, survivorship-aware backtest (`src/backtest/replay.py`),
  event-driven screen, four baselines (SPY-hold, random, RSI, P/E-ranking),
  metrics (Sharpe, maxDD, hit rate, vs-SPY).
- Multi-agent pipeline: fundamentals + technicals + sentiment → `synthesize()`.
- Disciplined data layer: EDGAR (PIT fundamentals + filing narrative), cached
  prices, PIT S&P 500 membership; signal- and filing-keyed caching; Batch API.
- Reproducibility scaffolding: per-kind cache versioning, full decision audit log.

**Fiction / missing (the gap to real money):**
- **Zero transaction costs / slippage** — every backtest number is currently
  non-decision-grade.
- **No out-of-sample / walk-forward discipline** — overfitting risk is unmeasured.
- **Risk management = one stop-loss.** No sizing model, no exposure/concentration
  limits, no drawdown governor.
- **No execution path** — no broker, no paper trading, no reconciliation.
- **No live-data pipeline, monitoring, or kill switch.**
- **No LLM-production guardrails** — model not pinned, no injection hardening,
  confidence can drive unbounded sizing.
- **Synthesis is a naive confidence-weighted vote**, not risk-aware.

---

## Phase 0 — Methodology integrity (DO THIS FIRST)

*Goal: make backtest results tell the truth, then find out if there's an edge.*

0.1 **Transaction-cost & slippage model.** ✅ **DONE (31 May 2026).**
`src/backtest/costs.py` (`CostModel`): adverse-fill slippage + commission
(bps/notional, per-share, min), applied at every fill in `Portfolio.open/close`
for *all* strategies (baselines included). Net-of-cost trade P&L; threaded through
`BacktestConfig.cost_model` and CLI flags (`--slippage-bps`, `--commission-bps`,
`--commission-per-share`, `--min-commission`); defaults model a liquid-large-cap
retail account (~5bps/side ≈ 10bps round-trip, zero commission). Frictionless by
default elsewhere, so prior behavior/tests are unchanged. **Market impact (size/ADV)
deliberately deferred to 0.5 (capacity)** — this model is size-agnostic.
Run defaults model the **real intended broker — IBKR Pro Fixed, US stocks,
Singapore account** ($0.005/share, $1 min, 1% cap; `CostModel.ibkr_singapore_fixed`)
plus ~5bps/side slippage. *Next: sweep cost levels in the costed run to find where
the edge breaks.*

0.2 **Dividends / total return.** Reinvest at ex-date close (FMP dividend data
already grabbed). Long-horizon results are wrong without it.

0.3 **Walk-forward / out-of-sample.** A protocol where any threshold or prompt
tuning happens on a train window and is *measured* on a held-out window. Track
how many independent bets the result rests on; treat <~50 as "cannot distinguish
skill from luck."

0.4 **Multiple-testing discipline.** Log every variant tried; report the edge with
that count in mind. Resist tuning synthesis until Sharpe looks good — that *is*
the overfit.

0.5 **Capacity & alpha-vs-beta.** Quantify how much of the return is just market
beta. For long-only large-cap, the honest question is whether *any* alpha remains
after 0.1. Estimate strategy capacity (how much capital before impact eats it).

> **GATE 0 (the honest gate):** Over a walk-forward window, *after costs and
> dividends*, the system shows a **statistically credible** excess return vs.
> SPY (risk-adjusted), resting on enough independent bets to not be noise.
> **If it fails here, stop — do not trade.** Iterate on the signal or accept that
> an index fund wins. Spending on bigger LLM runs before this gate buys a nicer
> illusion.

---

## Phase 1 — Risk management layer

*Goal: a layer between synthesis and execution that owns capital preservation.
It constrains; it does not vote.*

1.1 **Risk-based position sizing** — volatility-targeting or fractional-Kelly,
sized by risk contribution, not raw confidence. Hard per-position cap regardless
of model confidence.

1.2 **Portfolio constraints** — max gross/net exposure, per-name cap, sector and
factor (beta) concentration limits, basic correlation awareness.

1.3 **Drawdown governor / kill switch** — automatic de-risking past a threshold;
a manual master kill switch.

1.4 **Long-only vs. long/short decision** — determines whether we harvest beta or
target alpha / market-neutrality. Drives universe and sizing. *Open decision.*

> **GATE 1:** Backtest *with* the risk layer still clears GATE 0, and worst-case
> drawdown / exposure are within a pre-stated personal risk budget.

---

## Phase 2 — Execution & operations

*Goal: survive contact with a real broker before real capital.*

2.1 **Broker integration** — Alpaca or IBKR; order management (partial fills,
halts, market hours), corporate-actions handling.
2.2 **Paper trading for a meaningful period** (months). Reconcile paper fills vs.
backtest assumptions — this is where hidden look-ahead and cost optimism surface.
2.3 **Reconciliation** — does my modeled book match the broker's, every day?
2.4 **Monitoring / alerting** — position, P&L, data-staleness, and error alerts;
the kill switch wired to a human.

> **GATE 2:** Paper-trading performance tracks the backtest within tolerance over
> the test period (no nasty surprises in cost or timing).

---

## Phase 3 — LLM productionization & guardrails

*Cross-cuts all phases; must be solid before live capital.*

3.1 **Pin model IDs; treat a model upgrade as a code deploy** — re-run the full
validation suite on any model change. Model drift silently invalidates a
validated strategy. (Backtests are tied to a specific model.)
3.2 **Bounded sizing regardless of confidence** — the risk layer, not the LLM,
sets size; hard caps enforced downstream.
3.3 **Prompt-injection hardening** — filings (and later news) are
attacker-influenceable text. Sanitize, bound, and never let model output size a
trade without passing through the risk layer.
3.4 **Cost & latency budgets**, output-schema validation (largely in place via
forced tool use), determinism/repro, and the existing decision audit retained as
the post-mortem trail.

> **GATE 3:** No single LLM output can move capital beyond a hard, code-enforced
> bound; a model change cannot reach production without re-validation.

---

## Phase 4 — Live, small, scaled slowly

Start with capital you can fully afford to lose. Scale only as live results
confirm the backtest *and* the operational stack proves boring/reliable. Keep
paper and live running in parallel as an ongoing canary.

---

## Phase 5+ — Signal enhancements (only after the above)

Sequenced by value, and only once the edge is validated and risk-controlled:
- **Macro / regime specialist** — risk-on/off, rates, vol regime — to condition
  *exposure and sizing* (regime drives everything; highest-value addition).
- **Risk-aware supervisor** — graduate `synthesize()` from a confidence vote to a
  structured/LLM supervisor that weighs specialists *and* risk context.
- **Red-team / devil's-advocate agent** — argues against each trade; multi-agent
  debate measurably reduces overconfidence.
- **News specialist** — *only* with true point-in-time news (the unsolved data
  problem; without it, look-ahead bias invalidates the backtest). Deferred until a
  PIT-clean source exists.
- **Delisted-fundamentals wiring** (CIK map → EDGAR companyfacts) and
  **recycled-ticker disambiguation** — for a fully survivorship-free deep history.

---

## Cross-cutting / not to forget

- **Taxes** — frequent trading in a personal account triggers wash-sale rules and
  short-term rates that can dwarf the edge. Model after-tax return; consider
  account type and holding-period effects. *May change the whole calculus.*
- **Compliance** — PDT rule (if applicable), account minimums.
- **Data reliability** — live needs timely, correct feeds with failover; a data
  error is a real loss.
- **Psychological discipline** — a rule for whether/when a human may override the
  system (and the honest knowledge that discretionary overrides usually hurt).
- **Total cost of ownership / unit economics** *(defer; cost it later)* — beyond
  transaction costs, the *running* cost: LLM spend per rebalance at live cadence,
  market-data subscriptions, compute/hosting, monitoring, IBKR account/data fees.
  The real test is net-of-everything return on deployed capital. A strategy that
  beats SPY by a hair but costs $X/month in infra + LLM may still lose to the index
  on a small personal account. Size this against expected capital before going live.

## Kill criteria (when to stop — stated in advance)

- Fails GATE 0 (no credible edge after costs) → do not trade.
- Live/paper materially underperforms backtest with no explainable cause.
- Realized drawdown breaches the pre-stated risk budget.
- After-tax expected return does not beat a passive index for the risk taken.

## Open decisions needed from the operator

1. Long-only or long/short / market-neutral?
2. Universe (S&P 500? broader? liquidity floor?).
3. Initial capital and personal max-drawdown budget.
4. Broker (Alpaca vs. IBKR).
5. Rebalance cadence for *live* (vs. backtest) — costs and taxes argue slower.

---

## Immediate next step

Build **Phase 0.1 (transaction costs + slippage)** before spending further on
larger LLM runs, then re-run a modest window *with* costs to test GATE 0. That
spend then buys a decision-grade answer instead of a frictionless illusion.
