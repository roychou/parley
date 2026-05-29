# Backtest Design — Release 1

> Design surface document. Locked at end of Day 37 (Thu 28 May 2026). Drives implementation across Days 38–45.

---

## What the backtest is

The backtest replays the full supervisor pipeline — fundamentals specialist + technicals specialist + synthesis — over historical dates, paper-trades the resulting Decisions through a simulated portfolio, and measures performance against four baseline strategies.

It is an **evaluation harness for the decision pipeline**, not a trading system. No real execution, no live data, no risk management beyond an optional stop-loss. The point is to answer: *does the multi-agent system produce decisions that beat single-factor strategies and the market?*

## What the backtest is NOT

- Not a live trading system. No order placement, no real-time data.
- Not a strategy optimizer. We're measuring whether the existing pipeline beats baselines, not searching parameter space for the best variant.
- Not a proxy for live performance. Backtest results overstate live performance for well-known reasons (no slippage, no order book impact, perfect execution at close prices). Honest disclosure in the blog post.
- Not a calibration tool for confidence values. That's a separate Release 2 workstream — use backtest results to recalibrate the confidence-to-position-size mapping.

---

## Data layer changes — landed Day 37 (Thu 28 May)

The backtest depends on point-in-time-accurate fundamentals. The current yfinance-sourced data anchors on fiscal period-end (look-ahead bias of 60–90 days vs. actual filing dates). FMP migration is in scope for today before any backtest work.

**Migration scope:**
- Replace `fetch_yfinance_raw_fundamentals()` in `src/data/fundamentals.py` → `fetch_fmp_raw_fundamentals()`. Source: FMP's `/income-statement/` and `/balance-sheet-statement/` endpoints. Add `acceptanceDate` (filing date) to the returned dict.
- Replace `fetch_raw_history()` in `src/data/fetch_prices.py` → `fetch_fmp_raw_history()`. Source: FMP's `/historical-price-full/` endpoint.
- Update `ValuationSnapshot.report_date` semantics: was period-end, becomes filing date (`acceptanceDate`). The `period_end_date` becomes a separate optional field for reference.
- Re-run all 9 eval tests against FMP-sourced data to confirm calibration holds. Grounding evals may need minor prompt adjustments if FMP returns slightly different field names than yfinance.

**Out of scope today:** IBKR as data source. Defer indefinitely — FMP carries through to live trading; IBKR enters scope only as an *execution* venue when live trading enters scope.

---

## Decision pipeline being tested

For each (ticker, decision_date) pair the backtest produces:

```
fundamentals_specialist.run(ticker, as_of=decision_date) → FundamentalsAnalysis
technicals_specialist.run(ticker, as_of=decision_date) → TechnicalsAnalysis
synthesize(ticker, [fundamentals, technicals], as_of=decision_date) → Decision
```

The supervisor invocation runs the two specialists in parallel via `asyncio.gather` (the existing pattern). The Decision contains direction (BUY/HOLD/SELL), confidence, rationale, and contributing signals.

**Point-in-time discipline:**
- Fundamentals: specialist sees only filings with `acceptanceDate <= decision_date`
- Prices: technicals specialist sees only OHLCV for dates `< decision_date` (close-of-prior-trading-day discipline — no peeking at the current day's close)

The data layer is responsible for enforcing this. The specialists and supervisor remain unchanged.

---

## Cadence and universe

**Cadence:** weekly. Decisions made every Friday at close, for 26 decision points over a 6-month backtest window.

**Universe:** 15 tickers from `notes/universe.md`. If FMP has coverage gaps for any (e.g., ATZAF Canadian listing), substitute or drop and document.

**Backtest window:** 6 months ending 4 weeks before the present date (avoid look-ahead from any post-period information that may have been used during specialist development).

**Decision count:** 15 tickers × 26 weeks = 390 supervisor invocations. With caching by `(ticker, date)`, rerunning the backtest is free after the first pass.

**Cost estimate:** ~390 decisions × ~15 LLM calls per decision ≈ 5,800 calls. At Sonnet pricing (~$3/$15 per M tokens, ~1K input + 500 output per call typical), roughly $45–60 per full backtest run.

---

## Trade model

**Position sizing — confidence-weighted with floor and cap.**

| Parameter | Value | Rationale |
|---|---|---|
| `N_MAX` | 10 | Max concurrent positions |
| `BASE_ALLOCATION` | 10% | 1/N_MAX |
| `SIZE = BASE × confidence` | — | Scales position by signal strength |
| `FLOOR` | 2% | Skip signal if scaled position < 2%. Signal too weak to act on. |
| `CAP` | 15% | Max single position. Concentration risk control. |

Example: BUY@0.85 → 10% × 0.85 = 8.5% position. BUY@0.4 → 10% × 0.4 = 4% position. BUY@0.15 → would be 1.5%, below floor, signal skipped.

Confidence values are not yet empirically calibrated to outcomes — this is a known limitation, disclosed in the blog post. Release 2 will recalibrate using these backtest results.

**Exit rules — signal-driven.**

| Signal | Action |
|---|---|
| BUY | Open if no position; maintain size if position exists |
| HOLD | Maintain current state. Do not open new; keep existing open. |
| SELL | Close any existing position |
| Stop-loss (configurable) | Close if position is down -20% from entry. Default: enabled. |

No time-based exits. Real fundamentals strategies hold until the thesis breaks; arbitrary holding periods are backtest conveniences, not strategy features. If a position stays BULLISH for the entire 6-month window, it stays open. The backtest reports the final mark-to-market value of any open positions at end of window as if they were closed at the final-week close price.

**Long-only.** SELL closes existing positions; does not open shorts.

**Cash handling.** When no positions are open or when fewer than `N_MAX` positions are open, idle cash earns 0% (no money market modeling). Realistic refinement (T-bill yield) is a Release 2 enhancement.

---

## Baselines (four)

The multi-agent system needs to beat *both* the single-factor variants of its components to justify the architectural complexity. Each baseline runs against the same universe, cadence, and trade model.

**1. Random.** Each Friday, for each ticker without an open position, draw BUY/HOLD/SELL with equal probability. Sanity check — no information should produce performance indistinguishable from buy-and-hold of a random subset.

**2. Buy-and-hold SPY.** Buy SPY on the first decision date, hold to the end. The market benchmark. Active strategies must clear this bar to justify the trouble.

**3. RSI-based (technicals-only).** RSI < 30 = BUY, RSI > 70 = SELL, otherwise HOLD. Uses the same RSI-14 the technicals specialist computes. Tests whether the technicals specialist's reasoning adds value over its raw underlying signal.

**4. P/E ranking (fundamentals-only).** Each Friday, rank universe by trailing P/E. BUY the lowest-P/E quintile (3 tickers); SELL anything that drops out of the quintile. Classic single-factor value strategy. Tests whether the fundamentals specialist + multi-agent synthesis adds value over the strongest single-factor value approach.

**Position sizing for baselines:** equal-weight (1/N_open). Confidence-weighted sizing is unique to the multi-agent strategy because only it produces confidence values. Baselines using equal-weight is the apples-to-apples comparison — the multi-agent system's additional sophistication (confidence weighting) is part of what it has to justify.

---

## Metrics

**Per-strategy summary metrics:**

| Metric | Definition |
|---|---|
| Total return | Compound return over the full backtest window |
| Annualized return (CAGR) | `(end_value / start_value) ^ (1 / years) - 1` |
| Sharpe ratio | `(mean_weekly_return - risk_free) / stdev_weekly_returns × sqrt(52)`. Assume risk-free = 0 for Release 1. |
| Max drawdown | Worst peak-to-trough decline in portfolio value, expressed as % |
| Hit rate | Fraction of BUY signals that produced positive realized P&L when closed |
| Excess return vs SPY | Total return minus SPY total return over same window |

**Comparison table** in blog post: all five strategies (multi-agent + 4 baselines) across all 6 metrics, side by side.

---

## Per-decision logging

The per-decision audit trail is what makes the backtest a credible artifact rather than a black box producing summary stats. A reviewer must be able to ask "show me the BUY signal on NVDA in week 12" and see the full reasoning.

**Output format per run:** `data/backtest/runs/<run_id>/`

| File | Contents |
|---|---|
| `decisions.jsonl` | One line per Decision: ticker, decision_date, direction, confidence, rationale, fundamentals_signal, technicals_signal, fundamentals_reasoning, technicals_reasoning |
| `trades.jsonl` | One line per closed trade: ticker, entry_date, entry_price, exit_date, exit_price, exit_reason (SELL signal / stop-loss / end-of-backtest), position_size_pct, realized_pnl_pct |
| `equity_curve.csv` | Weekly portfolio value per strategy: date, multi_agent_value, random_value, spy_value, rsi_value, pe_rank_value |
| `metrics.json` | Summary metrics per strategy as defined above |
| `config.json` | The exact parameters used for this run: universe, window, cadence, sizing params, stop-loss setting, FMP data version |

`run_id` format: `YYYYMMDD_HHMMSS` for the start time of the run. Each backtest run is a new directory; reruns don't overwrite.

---

## Architecture

**New package: `src/backtest/`**

| File | Role |
|---|---|
| `replay.py` | Main backtest loop. Iterates dates × tickers, calls supervisor (with caching), records Decisions. |
| `strategies.py` | The 5 strategies: `MultiAgentStrategy`, `RandomStrategy`, `SPYHoldStrategy`, `RSIStrategy`, `PERankingStrategy`. Each implements `decide(ticker, date, history) → Decision`. |
| `portfolio.py` | `Portfolio` state machine. Handles `open_position`, `close_position`, `apply_decision`, `mark_to_market`. |
| `metrics.py` | Pure functions: `total_return`, `cagr`, `sharpe`, `max_drawdown`, `hit_rate`. Take an equity curve and trade list, return scalars. |
| `cache.py` | Specialist output cache by `(ticker, decision_date)`. Disk-backed JSON. Keyed on specialist version + date. |
| `runner.py` | Top-level entry point. Reads config, runs all 5 strategies in parallel, writes output directory. |

**Tests:** `tests/backtest/` mirrors the structure. Unit tests for metrics (pure functions, easy), state-machine tests for portfolio (deterministic), smoke tests for the replay loop (cached fixtures, no live LLM calls).

**Data flow per decision date T:**

```
For each ticker in universe:
    fundamentals_data = data.get_fundamentals(ticker, as_of=T)  # filtered to acceptanceDate <= T
    technicals_data = data.get_prices(ticker, as_of=T)          # filtered to date < T
    decision = cache.get_or_compute(ticker, T, lambda: supervisor.run(ticker, fundamentals_data, technicals_data, T))
    portfolio.apply_decision(decision, prices[T])
portfolio.mark_to_market(prices[T])
```

The `cache.get_or_compute` is the cost discipline — first run pays the LLM cost, every subsequent run is free.

---

## Architectural fork: live vs backtest supervisor

The live supervisor (`src/supervisor.py`) routes specialist data access through MCP — each specialist autonomously calls `get_fundamentals(ticker)` or `get_technicals(ticker)` via stdio subprocess. MCP's value is *specialist agency in data fetching*.

The backtest supervisor (`src/backtest/backtest_supervisor.py`) bypasses MCP entirely. For each (ticker, as_of) request, it:
1. Fetches point-in-time fundamentals + technicals from the data layer directly (using filing-date-anchored lookups in `get_fundamentals_as_of` and date-windowed indicators in `get_technicals_as_of`).
2. Calls each specialist in a single LLM turn, with data injected into the user message and `submit_analysis` forced via `tool_choice`.
3. Synthesizes via the existing `synthesize()` function.

**Why the fork:** In backtest, the data is known and point-in-time-filtered up front. The specialist's agency in choosing what to pull doesn't translate. Forcing MCP through env-var-injected as_of context would add ~24 subprocess spawns per supervisor call (~2–12s overhead) for no reasoning-quality gain — the specialist would call the same tool with the same args every time. The bypass is deliberate.

**What stays the same:** Specialist system prompts (`FUNDAMENTALS_ROLE_PROMPT`, `TECHNICALS_ROLE_PROMPT`) are reused verbatim. The output schemas (`FundamentalsAnalysis`, `TechnicalsAnalysis`) are identical. The synthesis function is the same. The reasoning being evaluated is the same.

**What's different:** The agent loop is collapsed from multi-turn (fetch → submit) to single-turn (data-in-context → submit). User-message instructs the model to "skip the get_X step and proceed directly to submit_analysis." The grounding/synthesis evals continue to validate the live MCP path; the backtest validates the reasoning pipeline on the same prompts with different data delivery.

**Cost implication:** ~2 LLM calls per supervisor invocation vs. ~10 in the live MCP path. For the planned 6-month × 15-ticker × weekly run = 390 supervisor calls × 2 = 780 LLM calls. At Sonnet pricing (~1.5K input + ~600 output per call), projected cost is ~$9–12 per full backtest run. Materially lower than the original design-doc estimate of $45–60 (which assumed the full MCP agent loop).

## Known limitations (disclose in blog post)

These are honest, not embarrassing. Disclosing them is part of the methodology being rigorous.

1. **No transaction costs.** Real trades pay spread, commission, and slippage. A weekly-cadence strategy on liquid US equities probably loses 5–15 bps per round-trip. Not modeled. Release 2 enhancement.
2. **Perfect execution at close prices.** Real fills happen at intraday prices that can differ materially from the close, especially in volatile names. Not modeled.
3. **Confidence values are not empirically calibrated.** The position sizing uses confidence values that were never validated against actual outcomes. The backtest produces the first data that can be used for calibration.
4. **No survivorship bias adjustment.** Universe is 15 tickers selected as of present-day. Tickers that delisted during the historical window are not represented. For a 6-month window with mostly large-caps, this is negligible. For multi-year windows it matters.
5. **Single-factor baselines use the same universe.** P/E ranking and RSI baselines pick from the same 15 tickers. A more rigorous baseline would let them pick from the full S&P 500. Acknowledged scope limit. *(Release 2: point-in-time S&P 500 universe + candidate screen — see `notes/universe-design.md`. Release 1's curated 15 is a watchlist, labeled as such.)*
6. **No dividend reinvestment modeled.** Returns are price returns only. For a 6-month window on growth-tilted names, dividend yield is ~1% — small but real. FMP provides dividend data; can be added.
7. **Synthesis is deterministic in Release 1.** The multi-agent strategy's confidence calibration is therefore mechanical (`min(abs(weighted_score), 1.0)`), not informed by an LLM supervisor that could express true uncertainty. Release 2 replaces this with LLM synthesis.

---

## Open questions for implementation

1. **FMP coverage of universe.** ATZAF (Aritzia, TSX), MOG-A (Moog Class A), PRYMY (Prysmian ADR) may have data gaps. Surface during the FMP migration today; substitute or drop ticker if uncovered.
2. **Backtest window exact dates.** Probably Nov 2025 through Apr 2026 (6 months ending 4 weeks before today). Confirm at start of replay implementation — depends on FMP historical depth on free tier.
3. **Stop-loss exact threshold.** -20% is the default. Should be a config parameter so the blog post can report sensitivity (e.g., "we tested -10%, -20%, -30%; -20% was best.").
4. **Cache invalidation policy.** Cache keys should include specialist version (a hash of the system prompt or a version string). When a specialist prompt changes, old cache entries are stale. Implement explicit versioning to prevent silent stale-data reuse.
5. ~~**Price-history depth at the loader seam (Day 43 blocker).**~~ **Resolved.** The fix turned out to be larger than a pure seam change: the 1y default funnels through `get_prices`, which is also called internally by *both* `get_fundamentals_as_of` and `get_technicals_as_of`, and the price cache was period-blind (a 1y cache silently satisfied a deeper request — a latent correctness bug). Fix: (a) `get_prices`/`process_ticker`/cache are now period-aware (cache key includes the period); (b) the point-in-time as-of loaders default to `price_period="5y"`; (c) `run.py` passes `partial(get_prices, period="5y")` as the replay `price_loader`. Verified: `get_fundamentals_as_of('MSFT','2024-01-15')` now returns the 2023-07-27 filing (was `None`); technicals trailing history reaches back ~2.5y.

---

## Implementation sequencing (Days 38–46)

> **Re-baselined Fri 29 May.** Days 38–41 were pulled forward and completed by 28–29 May, ahead of the original one-milestone-per-day estimate. Day 42 is now today's work. Remaining dates re-anchored from today; ship date held at Fri 12 Jun, so the buffer grew. "Day N" is a milestone counter, not a calendar day — the dates are estimates.

| Day | Scope | Status |
|---|---|---|
| 37 (Thu 28 May) | Data layer: FMP migration (fundamentals + prices), point-in-time filing dates via `ValuationSnapshot.report_date`. | ✅ Done |
| 38 (by Fri 29 May) | FMP migration verified live; all 9 eval tests re-run green against FMP data. | ✅ Done |
| 39 (by Fri 29 May) | Backtest skeleton: `portfolio.py`, `metrics.py` (pure functions, fully tested). | ✅ Done |
| 40 (by Fri 29 May) | `strategies.py` — all 5 strategies on the `decide_all()` interface. Multi-agent takes an injectable `decision_provider` (stubbed in tests). | ✅ Done |
| 41 (by Fri 29 May) | `replay.py` + `cache.py` + `backtest_supervisor.py`. Unit-tested. | ✅ Done |
| 42 (Fri 29 May — today) | Wire the real cached `run_backtest_supervisor` in as `MultiAgentStrategy`'s `decision_provider` in a runnable entrypoint. First small-window run (1 month, 3 tickers) to validate end-to-end. | ◀ In progress |
| 43 (Mon 1 Jun) | Full 6-month, 15-ticker run. Analyze outputs. Fix bugs. | |
| 44 (Tue 2 Jun) | Metrics validation, comparison table generation, sanity checks on baselines. | |
| 45 (Wed 3 Jun) | Blog post draft. | |
| 46 (Thu 4 Jun) | Blog post polish, repo README update, release tag. | |
| 47–52 | Buffer / final pass. Release 1 ships Fri 12 Jun (Day 52). | |

Slack: ~6 days buffer before the ship date after the re-baseline. If FMP coverage or any single sub-task burns more than expected, the slack absorbs without slipping ship.

---

## Success criteria for Release 1

The backtest is "shippable" when:
- All 5 strategies run end-to-end without error on the full 6-month, 15-ticker window
- Per-decision audit trail is complete and inspectable
- Summary metrics computed for all 5 strategies
- Comparison table is publishable
- Blog post draft exists explaining methodology, results, and limitations honestly

The result of the multi-agent strategy vs. baselines is **not** a success criterion. The point of Release 1 is shipping a defensible backtest, not winning the backtest. If the multi-agent strategy loses to RSI on this universe and window, the right response is to report that honestly — it's a more compelling artifact than fabricated outperformance.
