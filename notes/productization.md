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
- **Training-data contamination (the big one).** LLM decisions over in-training-window
  dates measure the model's *memory* of those tickers, not a strategy. Unaddressed by
  data hygiene — see 0.0. The headline blocker for any edge claim.
- ~~Zero transaction costs / slippage~~ — **resolved (0.1)**: cost/slippage model,
  IBKR-Singapore defaults, swept via CLI.
- ~~No total return~~ — **resolved (0.2)**: dividends credited on ex-date.
- ~~No out-of-sample / walk-forward discipline~~ — **partly resolved (0.3/0.4)**:
  tuning-axis OOS split + run log. (Does NOT cover the contamination axis — see 0.0.)
- ~~Risk management = one stop-loss~~ — **resolved (Phase 1)**: `src/risk.py` —
  inverse-vol sizing, hard per-name cap, max-gross, drawdown governor/kill switch.
  (Sector limits + long/short still open.)
- **Execution path: partial** — forward paper harness + IBKR price/news adapters
  built (mock-tested); IBKR execution adapter + live validation + monitoring remain.
- ~~No LLM-production guardrails~~ — **mostly resolved (Phase 3)**: model pinning
  (3.1), bounded sizing via the risk cap (3.2), prompt-injection hardening (3.3),
  spend cap. Live-data monitoring/kill-switch (ops) remain.
- **Synthesis is a naive confidence-weighted vote**, not risk-aware.

---

## Phase 0 — Methodology integrity (DO THIS FIRST)

*Goal: make backtest results tell the truth, then find out if there's an edge.*

0.0 **Temporal validity — training-data contamination (THE foundational gate).**
⚠️ **Open; reframes everything below.** When an LLM makes the decisions, a backtest
over dates *inside the model's training window* is contaminated **by construction**:
the specialist models (Sonnet 4.6 / Haiku 4.5, cutoffs in 2025) have ingested what
these tickers did — news, earnings recaps, "best stocks of 2024" narratives. The
leakage is **in the weights, not the inputs**, so our careful point-in-time *data*
hygiene (EDGAR filing dates, as-of prices) does **not** fix it. An in-window backtest
measures *memory + strategy*, confounded — and it inflates the **multi-agent** line
specifically (the deterministic baselines are clean), i.e. exactly the comparison we
care about. A senior reader discounts an in-window LLM headline return heavily.

Crucially, this is a **different axis** from 0.3's split (which is in-/out-of-sample
relative to *our tuning*). A 2024→2026 walk-forward is entirely inside the model's
knowledge — every fold is contaminated. The two axes are independent; you need both.

**Built so far (31 May 2026):**
- `src/backtest/temporal.py` + `--model-cutoff` / `--clean-only` operationalize the
  contamination split — the runner reports clean vs. contaminated decision dates, warns
  loudly when results are contaminated (engineering validation, not edge), and
  `--clean-only` restricts to the post-cutoff window.
- **Anonymization probe** (`--anonymize`): strips ticker + all date fields from the
  numeric specialists (neutral prompt, identity restored on output for bookkeeping) so
  they reason from figures alone; signals cache under a separate `-anon` version. Run
  named vs. `--anonymize` and diff the returns to *size the training-memory gap*.
  Sentiment is force-disabled under it — the filing narrative names the company and
  can't be anonymized (the most contaminated, least-anonymizable specialist).
*Still open:* verify the actual model cutoffs, and forward paper trading.

**Verified cutoffs (31 May 2026, Claude models overview):** Sonnet 4.6 (the
fundamentals/technicals/sentiment-synthesis model) — reliable Aug 2025, **training-data
Jan 2026**; Haiku 4.5 (sentiment map leaves) — training-data Jul 2025. Use the
*training-data* cutoff for contamination (weights encode all of training, not just the
reliable-recall window). The binding boundary is **Jan 2026** (Sonnet 4.6 makes the
judgments); `temporal.DEFAULT_MODEL_CUTOFF = 2026-01-31`, now the runner default.

**The brutal consequence:** against our ~May-2026 data edge, the clean (post-training)
window is only **~Feb–May 2026 — about 4 months, ~4 monthly rebalances.** That is far
too few independent bets for significance. **A backtest essentially cannot establish an
edge for this strategy.** This isn't a tuning problem; it's structural. Accept it.

What a *clean* test actually requires:
- **Post-cutoff window only** — `--clean-only` restricts to dates after Jan 2026. The
  only uncontaminated backtest, but ~4 months → no statistical power. Useful as a
  *sanity check* (does the clean slice at least not lose money?), not an edge proof.
- **Forward paper trading (Phase 2) is the gold standard** — every live decision is on
  data the model has never seen. Slow, but the only fully clean edge evaluation.
- **Anonymization probe** — feed "Company A" + numbers, no ticker/date; compare named
  vs. anonymized returns to *measure* the contamination gap (imperfect; a real product
  knows the ticker, but it quantifies the leak).

**Demote the in-window backtest to engineering validation** (does the pipeline produce
sane, well-formed decisions?), *not* edge evidence. For the **product track** this
matters less than it sounds — live trading is always post-cutoff, so contamination
corrupts the backtest-as-predictor, not live performance; gate the edge claim on
post-cutoff + forward paper. For the **product track**, the senior move is to
*surface* this analysis (post-cutoff result + anonymization gap + why forward paper
beats the backtest) — understanding the problem is a stronger signal than any return.

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

0.2 **Dividends / total return.** ✅ **DONE (31 May 2026).** `src/data/dividends.py`
loads the grabbed FMP dividend cache (split-adjusted `adjDividend`, keyed by ex-date);
the replay loop credits held positions on the ex-date via `Portfolio.apply_dividends`
(cash, as in a real account — redeployed on the next rebalance, no auto-DRIP). Verified
our prices are split-adjusted-but-not-div-adjusted (NVDA 10:1, no discontinuity), so no
double-count. Equity curve (hence Sharpe/return) is now total-return; summary reports
dividends received. Off when no loader passed (price-return only) — tests unchanged.

0.3 **Walk-forward / out-of-sample.** ✅ **DONE (31 May 2026).**
`src/backtest/validation.py` slices a single run's equity curve + trades at a split
date into in-sample (≤) / out-of-sample (>) — equity by snapshot date, trades by
entry_date (the decision) — and reports metrics per segment (`--oos-split DATE` or
`--oos-frac`). Surfaces the out-of-sample **bet count** and warns when it's below a
significance floor (`MIN_MEANINGFUL_BETS=30`). The protocol is procedural: tune on
in-sample, judge on the OOS window you commit NOT to touch. *Single split is the
MVP; rolling multi-fold walk-forward is the future extension.*
**Scope caveat:** this axis guards against *our* overfitting-by-iteration only. It
does **nothing** against the model's training-window leakage (0.0) — both folds can be
fully contaminated. Don't read a clean OOS split here as a clean edge.

0.4 **Multiple-testing discipline.** ✅ **DONE (31 May 2026).**
`src/backtest/runlog.py` appends one record per run (config + git state + headline
metrics + optional `--run-note`) to `data/experiments/runlog.jsonl`, and reports the
running count — so the number of variants behind any "edge" stays visible. Procedural,
not statistical: pair it with the 0.3 OOS split. (`--no-runlog` to skip.)

0.5 **Capacity & alpha-vs-beta.** ✅ **alpha/beta DONE (31 May 2026).**
`metrics.alpha_beta` regresses strategy returns on SPY (CAPM-style): reports beta,
annualized alpha, R², and n — the decisive "is it skill or levered beta?" test,
shown per strategy in the summary. **Capacity:** non-binding at personal-account
scale (a $10k position in a $100M+/day-ADV large-cap is ~0.01% participation — no
impact), so deferred; revisit with an ADV-participation + market-impact model only if
capital grows large (ties to the impact term left out of `CostModel`).

> **GATE 0 (the honest gate):** On **temporally valid** data — decision dates
> *after* the specialist models' training cutoff (0.0), and/or forward paper
> trading — *after costs and dividends* (0.1/0.2), the system shows a credible
> risk-adjusted excess return vs. SPY with **positive alpha, not just beta** (0.5),
> resting on enough independent bets to not be noise (0.3/0.4).
> **If it fails here, stop — do not trade.** And note the order: an edge measured on
> *contaminated* (in-training-window) data is not an edge, however good the costs and
> OOS hygiene look — temporal validity (0.0) is the precondition for this gate, not an
> afterthought. The in-window backtest is engineering validation only. Spending on
> bigger in-window LLM runs before a post-cutoff/forward design buys a nicer illusion.

---

## Forward paper-trading harness — BUILD STATUS (as of 31 May 2026)

Forward paper trading is GATE 0's only clean evaluation (every decision is on
post-cutoff, unseen data) and it accrues only with calendar time — so the harness is
being built to start the clock ASAP. **Vendor: IBKR** (data + free Benzinga news +
later execution; Singapore-resident, Alpaca not available). **Cadence: weekly,
point-in-time** — the intraday/real-time pivot was considered and rejected (an LLM's
seconds-latency can't win the speed game; its edge is slow synthesis — see the
"contemplating the pivot" discussion). Deploys laptop-first (weekly job, minutes);
persistent infra (VM + IBC + cron) only when going real-money.

**Built + tested (vendor-agnostic parts fully; IBKR IO mock-tested only):**
- `src/forward/paper.py` — `PaperBook` (JSON-persisted account: cash, positions,
  trades, equity, dividends, decision log) + `run_forward_step` (dividends → MTM →
  translate+execute), reusing `Portfolio` and the multi-agent sizing.
- `src/forward/decide.py` — `run_forward_decision`: fundamentals + technicals +
  sentiment + **news** → `synthesize`. Forward-only news; **news is an explicit
  toggle for the on/off ablation** (let the live record decide if news earns its keep).
- `src/forward/session.py` — `run_forward_session`: screen → decide → execute →
  persist for one date. The harness the adapters plug into.
- `src/agents/news_specialist.py` — news specialist (`NewsAnalysis`) +
  `combine_news_sources` (multi-source merge; curated feeds only, no open-social).
- `src/forward/run.py` — the **wiring entrypoint**: connect IBKR -> refresh caches ->
  build the decision provider (budget-capped, batch) -> run_forward_session -> save.
  `python -m src.forward.run --max-llm-usd N`. Cache-derived helpers tested; live
  orchestration needs a Gateway.
- `src/forward/ibkr.py` — IBKR **price** (`refresh_price_cache`) + **news**
  (`fetch_news_for` + `news_source_from_store`) adapters, refresh/ingest pattern,
  host/port-configurable (env). Pure converters unit-tested; **ib_async IO needs LIVE
  validation against a Gateway (CI has none).**
- LLM **spend cap** (`--max-llm-usd`, `src/backtest/budget.py`) — added after a
  validation run overran to ~$120; aborts at the cap, resumes from the warm cache.

**Remaining to run forward:**
1. **Operator setup** — IB Gateway (paper) + US market-data subscription (~$10/mo) +
   Anthropic credits topped up.
2. **Live validation** — run a session, confirm IBKR price/news adapters return real
   bars + headlines, reconcile sizing/execution.
3. **Later** — IBKR **execution** adapter (place paper orders) + scheduler (cron/launchd).

**Immediate next step (next session):** operator spins up IB Gateway (paper) + data
sub; then build the wiring entrypoint and **validate the IBKR price/news adapters live
on a couple of tickers**, then start the weekly forward clock (with the news on/off
ablation running from day one).

---

## Phase 1 — Risk management layer

*Goal: a layer between synthesis and execution that owns capital preservation.
It constrains; it does not vote.*

1.1 **Risk-based position sizing** — ✅ **DONE (1 Jun 2026).** `src/risk.py`:
inverse-vol sizing (per-position vol target / σ), confidence only as a tilt, hard
per-name cap. Integrated into backtest + forward sizing, opt-in via `--risk`.
*Kelly is deliberately NOT used* — it needs an edge estimate we don't have and is
fragile; fractional-Kelly is a post-forward-calibration option.

1.2 **Portfolio constraints** — ✅ **partial.** Max-gross (pro-rata scale-down, no
leverage) + per-name cap done. Sector/factor concentration limits **deferred** (needs
a sector map we don't have).

1.3 **Drawdown governor / kill switch** — ✅ **DONE.** `drawdown_derisk_multiplier`
tapers new risk from a soft threshold (−10%) to zero at the hard kill (−20%).

1.4 **Long-only vs. long/short decision** — *Open.* Long-only stands (the system
trades long BUY/SELL); long/short is a clean extension when decided.

> Note: GATE 1 needs a (credit-gated) run; the risk layer is built + tested offline.

> **GATE 1:** Backtest *with* the risk layer still clears GATE 0, and worst-case
> drawdown / exposure are within a pre-stated personal risk budget.

---

## Phase 2 — Execution & operations

*Goal: survive contact with a real broker before real capital.*

2.1 **Broker integration** — **IBKR chosen** (Singapore; Alpaca needs US residency).
Data adapters (price + news) **built** (`src/forward/ibkr.py`, refresh/ingest, via
`ib_async`); **execution adapter remains** — order management (partial fills, halts,
market hours), corporate-actions handling. All pending live Gateway validation.
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

3.1 **Pin model IDs; treat a model upgrade as a code deploy** — ✅ **DONE (1 Jun 2026).**
`src/models.py` is the single registry (IDs, training cutoffs, prices); budget/temporal/
specialists read from it, so a model change is one deliberate edit = the re-validation
chokepoint. Each run records the pinned IDs (runlog).
3.2 **Bounded sizing regardless of confidence** — ✅ **DONE.** The risk layer's hard
per-name cap bounds size regardless of model confidence.
3.3 **Prompt-injection hardening** — ✅ **DONE (1 Jun 2026).** `src/agents/safety.py`:
untrusted filing/news text is delimited + the model is instructed to treat it as data,
never instructions (spoofed delimiters stripped). Defense-in-depth with forced tool
output + the risk cap → a successful injection is at worst one bounded skewed vote.
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

- Fails GATE 0 (no credible edge after costs, **on temporally valid data**) → do not trade.
- The edge exists only *in-training-window* and vanishes post-cutoff / in forward paper
  → it was the model's memory, not a strategy. Do not trade.
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

Phase 0.1–0.5 are **done** (costs, dividends, walk-forward, run log, alpha/beta).
The gating work is now **0.0 — temporal validity**, which reframes the costed run:

1. **Identify the post-cutoff window.** Confirm the specialist models' training
   cutoffs; the clean decision dates are those strictly after. Expect ~6–12 months
   against our May-2026 data edge.
2. **Design the GATE-0 run as post-cutoff-only** (short, low-power, but *clean*), and
   plan the **anonymization probe** (named vs. anonymized) to size the contamination
   gap on the in-window data.
3. Treat any in-window backtest as **engineering validation**, not edge evidence.
4. The real edge verdict comes from **forward paper trading** (Phase 2) — start that
   clock as early as possible, since it's the only fully clean evaluation and it only
   accrues with calendar time.

Do not spend on a large in-training-window run expecting an edge answer — it can't
give one.
