# Synthesis Eval — Learning Surface + Parley Sketch

> Day 29. Sources read: Agent-as-a-Judge (arXiv 2508.02994), Multi-Agent-as-Judge / MAJ-Eval (arXiv 2507.21028), Pydantic AI LLMJudge docs.

---

## Source 1 — Agent-as-a-Judge (arXiv 2508.02994)

**Core idea:** extend LLM-as-judge beyond final-output scoring. An agent judge observes intermediate steps, uses tools, and reasons over the action log — not just the terminal answer. This catches failures that are invisible in the output but present in the reasoning path.

**Key distinction from single LLM judge:**
- Single judge: reads output, assigns score. Biased toward style, can miss hidden logical errors.
- Agent judge: reads trajectory. Can recompute arithmetic, verify citations, check reasoning consistency across steps.

**Patterns:**
- Committee/debate models: multiple agent judges with distinct personas (analyst, critic, defender) argue, then aggregate. Diversity of roles matters — homogeneous agents converge and agree on wrong answers.
- Process evaluation: judge assesses reasoning chain, not just conclusion.
- Tool-using judges: for numeric domains, judge recomputes the math rather than trusting the LLM's self-report.

**Most useful insight for parley synthesis eval:**
The synthesis judge should evaluate process, not just output. The question is not "is BUY the right call" (unknowable) — it's "does the synthesis process handle the specialist inputs correctly?" That's an internal consistency check, not an outcome check.

**Failure modes this catches:**
- Logical inconsistency across multi-step reasoning
- Confidence mismatch with evidence
- Calculation errors (via tool-using judge)

---

## Source 2 — Multi-Agent-as-Judge / MAJ-Eval (arXiv 2507.21028)

**Core idea:** a coordinator synthesizes outputs from multiple specialist agents, each evaluating along their own dimension. Individual evaluation → in-group debate → aggregation. Mirrors how real teams evaluate work.

**Dimension decomposition:**
MAJ-Eval doesn't give the judge a single rubric. It extracts stakeholder perspectives from domain context, then assigns each perspective to a distinct evaluator agent. The agents evaluate independently, debate, then a coordinator aggregates.

**Aggregation options:**
- Weighted average of scores across agents
- Majority vote
- Debate-then-aggregate (15/20 stakeholder groups improved after debate)

**Most useful insight for parley synthesis eval:**
The synthesis in parley has exactly the MAJ-Eval problem in reverse: the coordinator (supervisor) synthesizes two specialist signals. A synthesis eval is asking the judge to evaluate the coordinator's output given the specialists' inputs. The judge needs to understand both specialist domains to flag when the synthesis mishandles a disagreement or inflates confidence inappropriately.

**What this implies for rubric design:**
Don't use a single judge with a single rubric. Use dimension-specific checks:
1. Does direction match weighted vote? (arithmetic check)
2. Is confidence calibrated to specialist agreement? (disagreement detection)
3. Does rationale reflect both specialists? (coverage check)

Each check has a clear pass/fail criterion. Aggregate to overall judgment.

---

## Source 3 — Pydantic AI LLMJudge

**How it works:**
LLMJudge wraps an LLM call with an explicit rubric. Returns pass/fail + reasoning, or numeric score, or both. Rubric must be specific — "does the response ground claims in the data?" performs better than "is this a good response?".

**Key pattern — separate judges per dimension:**
Rather than one rubric for everything, deploy one judge per evaluation dimension. Keeps output interpretable, makes failure diagnosis clear. This directly maps to parley's existing judge.py pattern (one judge call per eval, schema per eval).

**Structured output:**
Tool-use forcing pattern (already in parley's judge.py) ensures structured Pydantic output. Same approach works for synthesis eval.

**Rubric specificity rule:**
"Response directly answers the user question without hallucination" outperforms "good response". For parley: "The Decision's direction matches the confidence-weighted vote implied by the specialist signals" is more reliable than "the synthesis is reasonable".

---

## Parley Synthesis Eval — Sketch

### What synthesis does

`synthesize()` is currently deterministic:
- Score = sum(confidence × SIGNAL_TO_SCORE[signal]) / len(signals)
- BUY if score > 0.3, SELL if score < -0.3, HOLD otherwise
- Confidence = min(abs(score), 1.0)
- Rationale is auto-generated from a template

Because synthesis is currently deterministic, the direction and confidence are always "correct by formula." The eval question for the current stub is therefore:

> Does the Decision accurately reflect the specialist inputs, and does it handle disagreement correctly?

When synthesis becomes LLM-driven in Release 2, the eval becomes more interesting: the LLM supervisor has discretion in weighting, rationale, and confidence calibration. The eval framework built now should be designed to survive that transition.

### Three eval dimensions (rubric structure)

**Dimension 1: Direction alignment**
- Rule: `direction` must be consistent with the confidence-weighted score implied by the specialist signals.
- Check type: near-deterministic (can compute expected direction from inputs, compare to actual).
- Implementation: this can be a pure assertion, not an LLM judge, for the current stub.

**Dimension 2: Disagreement detection**
- Rule: when specialist signals conflict (one BULLISH, one BEARISH), confidence must not be high. A high-confidence BUY/SELL when specialists disagree is a synthesis failure.
- Check type: LLM judge or rule-based threshold. For current stub: `abs(score) < 0.3` when signals diverge means HOLD is expected and confidence will naturally be low. For LLM-driven synthesis: judge checks whether the rationale acknowledges the tension.
- This is the most interesting failure mode — "disagreement blindness."

**Dimension 3: Rationale coverage**
- Rule: the rationale must reference both contributing specialists and their signals. A rationale that ignores one specialist's output is incomplete synthesis.
- Check type: LLM judge. Rule: "the rationale explicitly references each specialist named in contributing_signals and their signal direction."

### Failure mode being caught

**Disagreement blindness:** supervisor outputs BUY with high confidence when fundamentals=BULLISH@0.9 and technicals=BEARISH@0.8. The weighted score is near zero — direction should be HOLD, confidence should be low. A synthesis that ignores the BEARISH signal and outputs BUY@0.85 has failed to synthesize; it has just echoed one specialist.

This is the synthesis analog of the grounding failure mode in specialist evals. Just as grounding catches "the specialist cited a metric that isn't in the data," disagreement detection catches "the supervisor ignored a signal that should have changed the outcome."

### Planted-failure test shape

```python
# Construct a Decision that contradicts what the math would produce
planted_signals = [
    FundamentalsAnalysis(
        specialist="fundamentals",
        ticker="AAPL",
        signal="BULLISH",
        confidence=0.9,
        reasoning="Strong fundamentals...",
        as_of="2026-05-20",
        # ...other fields
    ),
    TechnicalsAnalysis(
        specialist="technicals",
        ticker="AAPL",
        signal="BEARISH",
        confidence=0.8,
        reasoning="Technicals deteriorating...",
        as_of="2026-05-20",
        # ...other fields
    ),
]

# Planted contradiction: BUY with high confidence despite strong disagreement
bad_decision = Decision(
    ticker="AAPL",
    direction="BUY",        # Wrong — score ≈ 0.05, should be HOLD
    confidence=0.85,        # Wrong — should be ~0.05 given disagreement
    rationale="Strong bullish fundamentals indicate a clear BUY.",  # Wrong — ignores BEARISH technicals
    contributing_signals=planted_signals,
    as_of="2026-05-20",
)

result = await synthesis_eval(bad_decision, planted_signals)

# Assert on verdict + evidence-of-detection, not on judge's internal categorization
assert result.passed is False
assert "disagree" in result.reasoning.lower() or "conflict" in result.reasoning.lower() or "bearish" in result.reasoning.lower()
```

### What the judge prompt should say

System: "You are evaluating whether a multi-specialist synthesis decision correctly reflects its input signals. You are not judging whether the direction is fundamentally correct — you are judging whether the synthesis process handled the specialist inputs accurately."

User prompt construction:
1. Specialist inputs: each specialist's signal, confidence, and reasoning
2. The synthesized Decision: direction, confidence, rationale
3. Ask the judge to evaluate: (a) does direction align with the weighted signals? (b) if specialists disagree, is confidence appropriately low and disagreement acknowledged in rationale? (c) does rationale mention all contributing specialists?

### What this eval is NOT catching

- Whether BUY is the right call for the ticker (unknowable at eval time)
- Whether individual specialist reasoning is grounded (that's the specialist grounding evals)
- Whether the confidence calibration is well-calibrated against real outcomes (that's backtesting, Week 4+)

### Where it lives

`evals/synthesis/grounding.py` — parallel to the specialist grounding evals. Follows the same pattern: one eval module per flavor, imported by tests.

### Open question before implementation

The LLM-judge approach for disagreement detection is most useful for Release 2 when synthesis has LLM discretion. For the current deterministic stub, Dimension 1 (direction alignment) and Dimension 2 (disagreement detection) are actually computable as pure assertions — no LLM judge needed. Consider whether the Day 31 implementation should be:

(a) Pure assertion eval against the deterministic synthesis formula (simpler, faster, no API cost)
(b) LLM-judge eval designed for Release 2's LLM synthesis (more complex, but the eval doesn't need to change when synthesis upgrades)

Option (b) is the right call if you expect synthesis to go LLM-driven in Release 2. Option (a) is the right call if you want eval coverage now and can update the eval when synthesis changes.

**Recommendation:** Start with (b). The judge prompt for synthesis is straightforward — it's checking internal consistency, not domain truth. Writing it now means the eval survives the Release 2 upgrade without modification.
