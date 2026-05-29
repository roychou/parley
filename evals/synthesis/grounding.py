import json
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic

from src.evals.base import EvalResult
from src.evals.judge import judge
from src.schemas.signal import Decision

# ==========================================
# 1. THE JUDGMENT SCHEMA
# ==========================================

class SynthesisJudgment(BaseModel):
    direction_aligned: bool = Field(
        description="True if the Decision's direction (BUY/HOLD/SELL) matches what the specialist signals collectively imply."
    )
    direction_reasoning: str = Field(
        description="Why direction is or isn't aligned with the collective weight of the signals."
    )
    disagreement_handled: bool = Field(
        description=(
            "True if disagreement between specialists is handled correctly — "
            "low confidence and disagreement acknowledged in rationale when signals conflict. "
            "True if all specialists agree (no disagreement to handle)."
        )
    )
    disagreement_reasoning: str = Field(
        description="Which specialists conflict (if any) and whether the decision handles that conflict appropriately."
    )
    rationale_covers_all: bool = Field(
        description="True if the rationale references every contributing specialist by name or role."
    )
    rationale_reasoning: str = Field(
        description="Which specialists are referenced in the rationale and which (if any) are absent."
    )
    overall_passed: bool = Field(
        description="True only if ALL three dimensions pass: direction_aligned, disagreement_handled, and rationale_covers_all."
    )
    summary: str = Field(description="1-2 sentences explaining the overall verdict.")

# ==========================================
# 2. THE EVALUATOR
# ==========================================

class SynthesisGroundingEval:
    """Evaluates whether a synthesis Decision correctly reflects its specialist input signals."""

    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self.eval_name = "Synthesis_GroundingEval"

    async def run(self, decision: Decision) -> EvalResult:
        signals_list = [
            {
                "specialist": s.specialist,
                "signal": s.signal,
                "confidence": s.confidence,
                "reasoning": s.reasoning,
            }
            for s in decision.contributing_signals
        ]

        decision_dict = {
            "direction": decision.direction,
            "confidence": decision.confidence,
            "rationale": decision.rationale,
        }

        system_prompt = """You are evaluating whether a multi-specialist investment synthesis decision correctly reflects its input signals.

You will receive:
1. SPECIALIST_SIGNALS: outputs from individual specialist agents, each with a signal direction (BULLISH/BEARISH/NEUTRAL), confidence (0.0–1.0), and reasoning.
2. THE_DECISION: the synthesized decision — direction (BUY/HOLD/SELL), confidence, and rationale.

You are NOT judging whether the direction is fundamentally correct for the stock. You are judging whether the synthesis process handled the specialist inputs accurately.

Evaluate THREE dimensions:

DIMENSION 1 — Direction Alignment
Does the Decision's direction match what the specialist signals collectively imply?
- Weigh each specialist's signal by their confidence. BULLISH@0.9 + BEARISH@0.8 is nearly balanced (net ≈ 0.05) and implies HOLD, not BUY or SELL. BULLISH@0.8 + BULLISH@0.6 strongly implies BUY.
- direction_aligned is True if the direction is consistent with the collective signal weight.

DIMENSION 2 — Disagreement Handling
When specialists disagree (e.g., one BULLISH and one BEARISH):
- The Decision's confidence must be low (a high-confidence BUY or SELL when signals conflict is a synthesis failure — it ignores evidence).
- The rationale must acknowledge the disagreement between specialists.
- disagreement_handled is True if both conditions hold, OR if all specialists agree (there is no disagreement to handle).

DIMENSION 3 — Rationale Coverage
Every contributing specialist must be referenced in the rationale.
- rationale_covers_all is True if each specialist named in SPECIALIST_SIGNALS appears in the rationale (by name, role, or signal type).

overall_passed is True only if ALL THREE dimensions pass."""

        user_prompt = f"""SPECIALIST_SIGNALS:
{json.dumps(signals_list, indent=2)}

THE_DECISION:
{json.dumps(decision_dict, indent=2)}

Please evaluate."""

        judgment: SynthesisJudgment = await judge(
            client=self.client,
            system_prompt=system_prompt.strip(),
            user_prompt=user_prompt.strip(),
            response_schema=SynthesisJudgment,
        )

        dimensions_passed = sum([
            judgment.direction_aligned,
            judgment.disagreement_handled,
            judgment.rationale_covers_all,
        ])
        score = dimensions_passed / 3

        return EvalResult(
            eval_name=self.eval_name,
            passed=judgment.overall_passed,
            score=score,
            ticker=decision.ticker,
            details=judgment.model_dump(),
        )
