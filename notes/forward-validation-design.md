# Forward-validation design — getting an earlier, cheaper, contamination-free read

> Status: design + first spike landed 4 Jun 2026. The IC core (`src/backtest/ic.py`,
> 17 tests) is built; the score-all driver that feeds it is the next build.
> Governs the "expedite the forward record" thread in `_plans/local-notes.md`.

## The problem

Forward paper trading is the only contamination-free read on this strategy (every decision
is post-cutoff), but it accrues one weekly portfolio observation at a time — years-to-never
for statistical significance. The clean *historical* window is only ~16 weekly dates
(~Feb–May 2026; bounded by the Sonnet 4.6 Jan-2026 training cutoff, see
`src/backtest/temporal.py`). We need a read that (a) extracts more signal from those 16
clean dates than a portfolio Sharpe can, and (b) can **disqualify fast** — say "no edge"
months earlier than a P&L curve could.

This is not a substitute for the forward clock. It is a way to compress confidence and to
kill the project early if the signal is absent.

## Core insight: measure IC, not portfolio Sharpe

A portfolio Sharpe collapses each date's ~100 cross-sectional bets into **one** portfolio
return. Over 16 clean dates that's a 16-point return series — its standard error is so wide
the Sharpe estimate is noise. That is *why* Phase 0 concluded "a backtest cannot validate
this."

The Information Coefficient keeps the cross-section. On each clean date, rank-correlate the
model's **signed conviction** against each name's **forward return** across the whole
universe. That's ~100 observations per date, **~1,600 over the clean window — not 16**.
Then aggregate: mean IC across dates, and its t-stat (Grinold's fundamental law:
`IR = IC × √breadth`, breadth = names × dates, not dates alone).

Same clean window, ~100× the usable observations. IC is the single most
statistically-efficient lever we have, and it points the disqualify-fast direction: if
clean-window mean IC ≤ 0 with a tight CI, that is real negative evidence.

### The signal we already emit

parley already produces continuous, signed conviction — no new model work:
- `Decision.confidence ∈ [0,1]` and `Decision.direction ∈ {BUY,HOLD,SELL}`
  (`src/schemas/signal.py`).
- Signed conviction = `confidence × sign(direction)` (BUY=+1, SELL=−1, HOLD=0), which is
  exactly the net directional `score` synthesis already computes (`src/synthesis.py:44`).

IC consumes that directly.

## What's built (the IC core) — `src/backtest/ic.py`

Pure Python (no numpy/pandas — same discipline as `metrics.py`), fully unit-tested
(`tests/backtest/test_ic.py`, 17 tests incl. a planted-signal calibration test):

- `spearman(xs, ys)` — rank correlation via fractional ranks + Pearson.
- `forward_return(prices, as_of, horizon_days)` — close-to-close return from the price
  cache (`data/cache/{ticker}_max_*.json`), first trading day ≥ as_of, N days forward.
- `cross_sectional_ic(as_of, convictions, forward_returns)` — Spearman across names present
  in both; **abstentions are missing, not zero** (a declined name is missing data, not a
  neutral bet); returns `ICResult(as_of, ic, n)`.
- `ic_summary(results, horizon_days, spacing_days)` — mean IC, IC-IR, naive cross-date
  t-stat, and an `overlap_warning` flag.
- `select_non_overlapping(dates, horizon_days, spacing_days)` — thin dates to independent
  forward windows.

## What's next (the score-all driver — the one real build)

The forward harness and backtest both only score the **event-screened candidate set** and
only act on a subset. IC needs a signed conviction for **every name in a fixed universe on
each clean date** — a name parley declined is information. So:

1. **Score-all mode.** Run `run_backtest_supervisor` (point-in-time, MCP-bypass) over the
   full Nasdaq-100 on each clean date, emitting `(ticker → signed conviction)`, decoupled
   from the screen and from trading. Persist to `data/experiments/conviction/<as_of>.json`.
2. **Forward returns.** Compute per name from the price cache at horizon(s) h ∈ {1w, 4w}.
3. **IC report.** Per-date IC, then `ic_summary`; print mean IC, IC-IR, t-stat, breadth,
   and the overlap flag. Wire into `runlog.jsonl` as a first-class experiment record.

### Cost discipline (avoid repeating the ~$120 abandoned run)

- **Cheap signal first.** Compute IC on the deterministic-ish **fundamentals + technicals**
  signal alone (no sentiment/news LLM fan-out). If even the cheap signal shows no
  cross-sectional IC, stop — don't pay for sentiment.
- **Subsample** to a fixed liquid ~40-name subset for the first pass if needed.
- Only escalate to full-universe + all specialists if the cheap read earns it.

## How the other two ideas compose (not separate builds)

- **Staggered model-cutoff ladder (most novel).** Run the *same* score-all harness with
  older-cutoff models (e.g. an early-2024 Sonnet, Haiku Jul-2025) over *their own* longer
  clean windows. Disqualifier: compare each model's **clean-window IC vs its in-window IC**
  — the *gap* is the contamination signal, and it controls for older models being weaker
  analysts (a null isn't just capability). If IC persists across the ladder, the
  *methodology* has edge, not one model's memory. Multiplies clean breadth out of history.
- **Decompose edge = signal-quality × signal→return.** The grounding evals already grade
  signal quality; **IC *is* the signal→return half, measured directly.** "Decompose" =
  existing eval + this harness. Nothing new to build.

So all three of the local-notes's thread-3 ideas reduce to **one build: the score-all IC
harness.** The IC math is done; the driver is the remaining work.

## Honest caveats (do not over-read the t-stat)

1. **Overlapping forward windows.** Weekly decisions with multi-week horizons autocorrelate
   the per-date ICs and inflate the naive t-stat. `ic_summary` flags this; use
   `select_non_overlapping` (trades breadth for independence) or a Newey-West SE (future).
2. **Still contamination-bounded.** IC is only an edge claim on the **clean window**
   (post-cutoff dates per `temporal.py`). In-window IC is a contamination *probe*, not edge.
3. **Cross-sectional, not timing.** IC measures whether the model ranks names correctly on a
   date — it says nothing about market-timing or position sizing. That's the right thing to
   isolate first, but it's not the whole P&L story.
4. **Survivorship / universe drift.** Score against the point-in-time Nasdaq-100 membership
   for each date, not today's; the universe loader already supports this.
5. **Not a replacement for forward.** This compresses confidence and can disqualify early.
   It cannot, by itself, certify edge — the forward clock remains GATE 0.

## Empirical results so far (4 Jun 2026 — clean window, 3 monthly dates, 20-day horizon)

| Signal | mean IC | t-stat | per-date ICs | cost / time |
|---|---|---|---|---|
| fund+tech (thresholded conviction) | −0.117 | −0.61 | −0.18, −0.41, +0.24 | ~$8 / ~6 min (sync) |
| fund+tech (continuous score) | −0.086 | −0.43 | −0.15, −0.39, +0.29 | ~$8 / ~6 min |
| sentiment-only (Haiku, diff-only sensor) | −0.043 | −0.45 | −0.17, −0.11, +0.15 | **~$0.36 / ~70 s** |
| **fund+tech + sentiment (combined, z-sum)** | **−0.087** | **−0.55** | −0.20, −0.29, +0.23 | (reuses caches) |

**Read:** every signal is slightly negative and statistically indistinguishable from zero;
sentiment adds **no** measurable marginal ranking skill. This is a directional "no" (3 dates,
diff-only proxy), not a verdict — but it says *don't pour spend into scaling these signals
for cross-sectional ranking* over the clean window. The bottleneck is now **date count**, not
tooling → the model-cutoff ladder (below) is the live lever.

### Full-tool ladder result (Sonnet 4.5, 39 clean dates) — a consistent null

Ran the FULL tool (fundamentals + technicals + carry-forward **hybrid** sentiment) on
**Sonnet 4.5** (Jul-2025 cutoff) over **39 clean weekly dates** (Aug 2025 → Apr 2026), ~82-86
names/date, **3,212 cross-sectional observations**. (Sonnet 4.0's deeper ~55-date window was
unavailable — 404 on this account; the cheap deep rung is effectively gone.)

| Metric | Value |
|---|---|
| mean IC | **−0.0046** |
| t-stat | **−0.21** (overlap-inflated → true significance even weaker) |
| std of per-date IC | 0.136 (vs 0.34 at 3 dates — converged toward zero, not a masked signal) |
| per-date IC range | −0.36 … +0.23, centered on zero |
| cost / calls | ~$45-50 / 4,041 |

**Result: no measurable cross-sectional ranking edge over this window.** The full multi-agent tool does not
predict which Nasdaq-100 names outperform over the next ~20 trading days, over a real,
contamination-free, never-trained-on window. Adding dates *sharpened* the zero rather than
revealing signal. Consistent with every probe today (cheap signal, sentiment-alone, combined,
now full tool @ 39 dates): a consistent "no detectable edge" on ranking alpha. (No
minimum-detectable-effect was computed, so this rules out a *large* ranking edge over this
window, not a small one — absence of evidence, not proof of none.)

**Caveats (don't over-conclude):** (1) IC tests *cross-sectional ranking* only — not risk/
drawdown management, sizing, or timing, where the bot could still add value; (2) Sonnet 4.5,
not deployed 4.6 (4.6's clean window is only ~12 dates, untestable at length); (3) 20-day
horizon — a 1-week (news/momentum) horizon is a different, untested question.

**Implication:** do NOT reflexively tweak knobs to manufacture a positive IC (overfitting trap).
The honest open questions are different *tests*, not parameter hunts: (a) does the bot add value
via drawdown avoidance rather than ranking? (b) is there edge at a shorter horizon? The forward
clock remains the final arbiter; today materially lowered the prior of ranking alpha here.

Cost note: the Batch API was a **bad fit** for the sentiment scaffold — its multi-wave
map-reduce fired ~11 sequential batches, each paying ~3–5 min queue latency (>1 hr, killed).
Synchronous Haiku on the diff-only sensor did the same job in ~70 s for ~$0.36. **Batch is for
large set-and-forget sweeps, not iterative probes.**

## The carry-forward tone sensor (middle ground: diff-only ↔ full analysis)

Diff-only is cheap but blind to *standing* tone; full re-analysis keeps standing tone but
re-reads the whole filing every quarter. The middle ground is a **stateful tone estimator**
that carries tone forward across a company's filing chain:

- **State per company** = a compact "tone memo" (a few sentences: current standing tone + key
  themes) + a scalar tone level in [−1, 1].
- **Bootstrap** (first filing in chain): one **whole-section** read → seed memo + level. Full
  cost, but once.
- **Update** (each later filing): `(prior memo + level) + (this quarter's diff)` → updated memo
  + new level. After bootstrap it only reads the cheap diff, but keeps absolute tone because
  each diff is *integrated onto* the carried state, not read in isolation.

Structurally a recurrent summary / "tone Kalman filter": diff = measurement, memo = state.
Cost over N quarters = `1 full + N cheap` → amortizes toward diff-only on long chains while
keeping full's fidelity.

Design points:
- **Point-in-time clean by construction.** Process filings chronologically; the memo at date
  T only encodes filings with `filed ≤ T`. No look-ahead through the state.
- **Drift guard.** Memo errors propagate; re-anchor with a full whole-section read every Kth
  filing (e.g. every 4 quarters) to reset accumulated drift. Tunable cost/fidelity knob.
- **Enables the ladder.** The ladder runs long historical chains per company (years of
  filings); that's exactly where the one-time bootstrap amortizes to ~nothing and the carried
  tone stays faithful. (c) is the cost/fidelity engine that makes (b) affordable at scale.

## The model-cutoff ladder — manufacturing clean dates from history (the live lever)

The clean window is only 3–16 dates because today's decision models have a Jan-2026 cutoff.
The ladder manufactures *more* clean dates by running **older-cutoff models** over *their own*
post-cutoff windows (e.g. Claude 3.5 Sonnet, ~Apr-2024 cutoff → mid-2024→now = far more dates).

- Per rung: compute IC over that model's clean window AND its in-window (contaminated) window.
- **Disqualifier/confirmer:** compare each model's **clean-window IC vs its own in-window IC**.
  High in-window but ~zero clean ⇒ memory, not skill. IC that persists clean **across rungs**
  ⇒ the *methodology* has edge, not one model's recall.
- **Control for capability:** compare each model **to itself** (clean−contaminated gap), never
  across models in absolute terms — older models are weaker analysts; the gap nets that out.
- **Feasibility:** rungs live in `models.py` `LADDER_MODELS`; price `max` cache + EDGAR (~2009+)
  cover the history. Pair with the carry-forward sensor for cost.

This is the genuine clock-shortener: it trades "wait for calendar time" for "use older models'
already-elapsed clean windows."

### Verified ladder (platform.claude.com, fetched 4 Jun 2026 — re-verify before use)

Claude 3.x is **retired/unavailable** — earlier speculation of ~100–140 dates from Claude 3.5/3
was wrong. The realistic floor is the Claude-4.0 generation at a **Mar-2025** training cutoff.
Using the conservative training-DATA cutoff; "~dates" = clean weekly dates with a valid 20-day
forward return given the price cache ending ~28 May 2026.

| Model | API ID | Train cutoff | ~clean wk dates | Price in/out | Status |
|---|---|---|---|---|---|
| Sonnet 4.6 (deployed) | `claude-sonnet-4-6` | Jan 2026 | ~12 | $3/$15 | current |
| Sonnet 4.5 | `claude-sonnet-4-5-20250929` | Jul 2025 | ~38 | $3/$15 | legacy, durable |
| **Sonnet 4.0** | `claude-sonnet-4-20250514` | **Mar 2025** | **~55** | $3/$15 | **retires 15 Jun 2026** |
| Opus 4.1 | `claude-opus-4-1-20250805` | Mar 2025 | ~55 | $15/$75 | durable Mar-2025 rung |

**⚠ Time-sensitive:** Sonnet 4.0 is the cheap deep rung (~55 dates at $3/$15) but **retires 15
Jun 2026** — ~11 days from this note. After that the durable Mar-2025 option is Opus 4.1 at 5×
the price, or settle for Sonnet 4.5's Jul-2025 cutoff (~38 dates, cheap, not retiring). Judgment
lever (not pulled): the *reliable-knowledge* cutoff is Jan 2025 for the 4.0-gen and Sonnet 4.5;
trusting it instead of the training-data date would push windows back further but risks
contamination (leakage is in the weights, not the reliable-recall date). Stay conservative.
