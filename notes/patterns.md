# Patterns

Observations worth carrying forward. Not how-tos — those live in the code. These are behavioral patterns in the agents, design principles that proved out, and eval lessons learned.

---

## Eval patterns

**Decompose the judge's task explicitly.** Don't ask the judge to "evaluate the reasoning." Ask it to extract every factual claim in the reasoning, check each against the supporting data, then aggregate. Decomposed tasks produce more reliable LLM judgments than holistic ones.

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

**`Ticker.financials` columns are fiscal period-ends, not filing dates.** MSFT shows June year-ends; AAPL shows September. Period-end ≠ filing date — companies file 60-90 days after period close. For v1, data is anchored to period-end. For strict point-in-time backtests, this is a known limitation; yfinance doesn't expose actual filing dates.

---

## Workflow

**Budget heuristic: Sonnet for everything until burn rate forces a split.** The token logging convention (greppable `api_usage` lines at every call site) means cost can be derived from logs without modifying code. Don't optimize model selection before the data exists.

**Verify model strings.** Anthropic ships fast and cached model IDs go stale. Default Sonnet ID as of Day 1: `claude-sonnet-4-6`.

**Edge cases in grounding evals are still undertested.** Subtle mischaracterizations, partial groundings, and claims that are technically true but misleading are not yet covered by planted-failure tests. These are the hardest cases for the judge to catch and the most likely to surface in real specialist outputs.
