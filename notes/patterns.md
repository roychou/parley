# Patterns

Observations worth carrying forward. Not how-tos — those live in the code. These are behavioral patterns in the agents, design principles that proved out, and eval lessons learned.

---

## Eval patterns

**Decompose the judge's task explicitly.** Don't ask the judge to "evaluate the reasoning." Ask it to extract every factual claim in the reasoning, check each against the supporting data, then aggregate. Decomposed tasks produce more reliable LLM judgments than holistic ones.

**Use separate boolean dimensions, not a single rubric.** A single `passed: bool` tells you the eval failed. Separate booleans (`direction_aligned`, `disagreement_handled`, `rationale_covers_all`) tell you which part failed and what to fix. Each dimension has an independent fix: direction → synthesis weighting, disagreement → confidence calibration, coverage → LLM prompt. Collapsing them into one rubric requires parsing the judge's free-form summary to diagnose — re-introducing the string-parsing problem.

**Pair each boolean with a reasoning string in the schema.** `disagreement_handled: bool` + `disagreement_reasoning: str` forces chain-of-thought per dimension: the model must articulate its reasoning before committing to the boolean. This makes judgments more reliable and makes failures human-readable at a glance.

**Use negative framing in judge prompts when the evaluation boundary matters.** "You are NOT judging whether BUY is correct for the stock. You are judging whether the synthesis handled the inputs accurately." Without the negative, the judge anchors on unknowable market correctness. With it, the judge anchors on internal consistency — a checkable property. This is the rubric specificity principle.

**Include in-prompt arithmetic examples when the judge must reason about numeric thresholds.** "BULLISH@0.9 + BEARISH@0.8 is nearly balanced (net ≈ 0.05)" teaches the judge the weighting arithmetic. Without it, the judge may reason qualitatively ("both have opinions") rather than quantitatively. One concrete example is sufficient.

**Pydantic field descriptions are judge instructions.** The description on each schema field is what the judge reads when deciding how to fill it. Write descriptions as explicit evaluation criteria, not as documentation for Python readers. These are the rubric.

**LLM-judge over pure assertion when the eval must survive an LLM upgrade.** Current deterministic synthesis could be validated with pure math assertions. But Release 2 replaces the formula with LLM synthesis, after which "expected direction from formula" is not computable. A judge that reasons about signal weight semantically works for both stub and LLM synthesis — write it once.

**score = dimensions_passed / N, not just passed: bool.** `passed` is the gate. `score` is the trend metric. A system that improves 0.33 → 0.67 → 1.0 over time was improving even if `passed` was always `False`. Continuous score enables regression tracking without changing the eval contract.

**Project signals to a minimal dict before passing to the judge.** `FundamentalsAnalysis` carries P/E ratios, margins, etc. The synthesis judge doesn't need those — it needs specialist name, signal direction, confidence, and reasoning. Projecting to a clean dict keeps the prompt focused and prevents the judge from reasoning about irrelevant domain details.

**Planted-failure tests are calibration gates, not nice-to-haves.** Three all-pass results are consistent with both "the judge works" and "the judge rubber-stamps." A single planted contradiction distinguishes them. Every new eval flavor ships with one.

**Assert on verdict + evidence-of-detection, not on judge categorization.** LLM judges have legitimate discretion in how they decompose multi-clause reasoning. Over-specifying the categorization (e.g., asserting the judge labels a claim as PATTERN vs. DIRECTIONAL) produces flaky tests. Assert that `passed is False` and that the evidence of detection appears in the reasoning.

**Strictness is a feature when calibrating.** Keep the NUMERIC_INTERPRETATION rule strict on first implementation — it can loosen later if real runs surface false negatives. Loosening during calibration conflates "this claim is acceptable" with "the judge is too strict."

**Pattern verdicts on numerically close cases should be spot-checked.** The judge is an LLM, not a calculator. If a claim is "SMA-50 crossed above SMA-200" and the supporting data shows SMA-50 = 145.2 and SMA-200 = 147.8, the judge has to do the comparison itself. Sonnet gets this right most of the time but is not foolproof on edge cases with close values.

**Design before implementation, even when familiar.** The 5-minute sketch in eval-design.md before opening the technicals grounding eval produced the three-category decomposition (NUMERIC_INTERPRETATION, PATTERN, DIRECTIONAL/TEMPORAL) before any code was written. The same pattern would have taken longer to arrive at mid-implementation. One page of design before opening the editor is not overhead — it's the work.

**The DIRECTIONAL/TEMPORAL grounding rule has a schema gap.** The rule requires explicit lookback metadata (e.g., "20-day momentum") to evaluate trend claims. `TechnicalsAnalysis` has `date_range` but no per-indicator lookback window. Planted-failure tests confirmed the judge correctly flags trend claims without temporal grounding — so the gap doesn't break calibration. It becomes a real gap when the specialist makes legitimate trend claims that should be grounded but aren't.

---

## Agent behavior

**Specialist prompts do real work — don't underestimate them.** On TSLA (P/E 412), the specialist correctly identified the extreme valuation and didn't rationalize it. That's the prompt's threshold rules doing the work, not emergent model judgment. The same prompt on a borderline case may behave differently.

**Rounding near thresholds toward favorable — watch for it.** On MSFT, the agent treated 14.93% revenue growth as "effectively at" the 15% strong-growth threshold and used it as bullish evidence. Reasonable analyst behavior, but if this pattern skews toward the bull case more than the bear case, it biases signals systematically. Monitor across more tickers before concluding either way.

**Without `as_of` in tool results and current date in system prompt, the model invents date ranges.** Anchoring matters. Models hallucinate plausible-sounding dates when no anchor is provided.

**Tool descriptions function as prompts.** Vague tool descriptions produce default model behavior — the model picks whatever feels reasonable. If a tool has a `period` parameter and the description doesn't specify when to use 1mo vs. 3mo, the model will guess. Be explicit.

**Pydantic field descriptions on output schemas are model instructions.** When two fields need to agree (e.g., `as_of` matching the tool result's date), the cross-reference belongs in the field description, not just inferred from field names.

**System prompts steer better when structured into sections.** Role / workflow / rules / constraints as discrete sections outperforms continuous prose. Run-on prompts blur structure the model would otherwise latch onto.

**Conflict-resolution rules must be explicit.** When a specialist combines multiple signals, specify which wins when they disagree (e.g., "weight trend over momentum when conflicting"). Without it, the same inputs can produce different outputs across runs.

**`submit_analysis` is an exit signal, not a tool dispatch target.** Handle it inline in the agent loop with an early return; only true data-fetching tools route through `dispatch_tool`.

---

## Design principles

**Threshold rules over sector-comparison rules when the agent has no peer data.** The fundamentals specialist initially compared against "sector averages" it didn't have access to. It would hallucinate benchmarks or hedge to NEUTRAL. Replaced with absolute thresholds — cruder but eval-able and grounded. Sector context is a Release 2 improvement.

**Schemas should reflect the actual temporal shape of the data.** Added `date_range` to `FundamentalsAnalysis` because `TechnicalsAnalysis` had it — then stopped to ask what it would mean for a point-in-time filing snapshot. Answer: nothing useful. Schemas should not be retrofitted from another specialist's solution to a different problem.

**`Ticker.financials` columns are fiscal period-ends, not filing dates.** MSFT shows June year-ends; AAPL shows September. Period-end ≠ filing date — companies file 60-90 days after period close. For v1, data is anchored to period-end. For strict point-in-time backtests, this is a known limitation; yfinance doesn't expose actual filing dates. *(Resolved Day 37: migrated to FMP, which exposes `filingDate`. `ValuationSnapshot.report_date` now carries the actual filing date.)*

**Dependency injection at the LLM boundary.** When a code path is LLM-dependent, accept the LLM call as a parameter (callable, async). `MultiAgentStrategy` takes a `decision_provider` rather than calling the supervisor directly. Tests inject a stub; production injects the real cached supervisor. Translation-logic tests run in 0.02s instead of 80+ seconds of API calls, and wiring the production-real LLM is a one-line change at the integration seam.

**Declarative intent vs execution.** When a component needs to interact with a state machine, have it emit declarative objects (open this with this size, close this with this reason) rather than calling the state machine directly. Sizing rules differ per strategy; execution rules don't. The emitted objects are inspectable in tests without running the state machine. Same separation as "what does the model think?" vs. "what did the system do?" — both are meaningful, and the architecture should let you observe each.

**Pure-Python in tight loops, framework code at one-shot calls.** The existing `rsi()` function uses pandas; `compute_rsi` in `strategies.py` is pure Python. Same Wilder's-smoothing math, different access pattern. Pandas Series construction inside an inner loop adds per-call overhead; in the technicals specialist (called once per ticker per run) it doesn't matter. When a function is called N times in a tight loop, prefer Python primitives over framework objects. Two implementations exist intentionally.

**Threshold rules in a prompt are priming, not control flow — the difference is what makes this not a rules-based bot.** The fundamentals prompt reads like rules ("P/E above 40 is high", "low P/E + declining revenue + high D/E = value trap"), and a reader could mistake the whole system for a rules engine. The distinction: in a rules-based bot `if pe < 15: confidence = 0.8` *executes* — mechanical and total. In the specialist those lines *prime a judgment* — the model weighs P/E against growth, margin, D/E, and the conflict-handling instructions, then writes free-text reasoning and picks a confidence. Two tickers at the same P/E can land on different confidences because the surrounding evidence differs; a rule can't do that. Three things make the system categorically different from rules: (1) thresholds guide rather than control; (2) cross-specialist synthesis is interpretive, not arithmetic — conflicting signals must resolve to low-confidence HOLD with the disagreement *named*, not an averaged number; (3) it's evaluated like a judgment system (LLM-judge grounding evals scoring whether reasoning is grounded in inputs), not a calculator (unit tests on whether a rule fired). Caveat worth owning: heavy explicit thresholds mean the model behaves *close* to rules-based on easy cases — the differentiation lives in the conflict/ambiguous cases, which is exactly what the synthesis eval targets.

**The rules-based strategies are baselines, not the product.** RSI, P/E-ranking, random, and SPY-hold share the backtest with the multi-agent strategy precisely to measure the *marginal value of judgment over rules*. If a hard-coded P/E quintile matches the LLM, the LLM isn't earning its token cost. The honest claim is not "I built a trading bot" but "I measured whether agentic judgment beats single-factor mechanical rules" — `PERankingStrategy` is the null hypothesis, not a feature.

---

## Backtest patterns

**Sizing snapshot before mutations within a round.** When multiple OPEN actions execute in the same round, snapshot `portfolio.total_value(prices)` *once* before any are applied; all OPENs size against the snapshot. Without this, the first open shrinks cash and subsequent opens get smaller positions than the strategy intended. Silent compounding bug for strategies that emit multiple opens per period (P/E ranking opens up to 3 in one rebalance round).

**Closes execute before opens in mutation rounds.** When a strategy emits both CLOSE and OPEN actions in the same round, run closes first. P/E ranking's quintile rebalance is the canonical case: A and B drop out of the quintile, C and D enter. If opens ran first, `can_open` would return False because `max_positions` is still occupied by A and B. The strategy would silently fail to rebalance. Closing first frees the slots.

**Decisions are not actions; log both.** A HOLD signal is a decision (the model considered the case and chose not to act), not an action (no portfolio mutation). Audit trails need decisions; portfolios need actions. Mining the action list for decisions silently drops every HOLD from the audit. Strategies expose `last_decisions` separately from the action list; the orchestrator logs both. This surfaced when the first real-LLM validation produced 2 supervisor calls with 0 logged decisions because both were HOLDs (specialist disagreement).

**Architectural forks share core, diverge at boundaries.** The live supervisor (MCP-based, specialist agency in data fetching) and the backtest supervisor (direct injection, data known up front) share specialist prompts, output schemas, and the `synthesize()` function. They diverge only at the data-access boundary. When two access patterns need different shapes, fork at the boundary and preserve the core. Document the fork explicitly so future readers understand what's deliberate and what's inherited.

**Override prompt workflow in the user message, not the system prompt.** When a backtest variant needs the model to skip a step ("data already fetched, go straight to submit_analysis"), put the override in the user message — don't modify the system prompt. Modifying the prompt invalidates eval calibration. LLMs treat per-call user instructions as authoritative over generic system-prompt instructions; the override works.

**Version-namespaced cache for prompt-dependent results.** LLM decisions cached for replay depend on the prompt that produced them. Namespace the cache by a version string the user bumps when prompts change. Old entries persist on disk but are silently ignored by lookups under the new version. Forces the user to choose to incur re-run cost; prevents stale-prompt results silently feeding new analyses.

---

## Workflow

**Budget heuristic: Sonnet for everything until burn rate forces a split.** The token logging convention (greppable `api_usage` lines at every call site) means cost can be derived from logs without modifying code. Don't optimize model selection before the data exists.

**Verify model strings.** Anthropic ships fast and cached model IDs go stale. Default Sonnet ID as of Day 1: `claude-sonnet-4-6`.

**Edge cases in grounding evals are still undertested.** Subtle mischaracterizations, partial groundings, and claims that are technically true but misleading are not yet covered by planted-failure tests. These are the hardest cases for the judge to catch and the most likely to surface in real specialist outputs.
