import json
from typing import List, Literal
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic

from src.evals.base import EvalProtocol, EvalResult
from src.evals.judge import judge
from src.schemas.technicals import TechnicalsAnalysis # Assuming this is your output schema

# ==========================================
# 1. THE JUDGMENT SCHEMAS
# ==========================================

class TechnicalClaimVerdict(BaseModel):
    """Evaluates a decomposed technical claim."""
    claim_text: str = Field(description="The specific claim extracted from the reasoning.")
    claim_type: Literal["NUMERIC_INTERPRETATION", "PATTERN", "DIRECTIONAL"] = Field(
        description="Categorize the claim based on its structure."
    )
    verdict: Literal["GROUNDED", "UNGROUNDED"]
    explanation: str = Field(
        description="Why it is grounded/ungrounded. If ungrounded, explicitly state if the data was wrong, the interpretation was flawed, or the required metrics were missing."
    )

class TechnicalGroundingJudgment(BaseModel):
    claims: List[TechnicalClaimVerdict]
    overall_passed: bool = Field(description="True only if ALL claims are GROUNDED.")
    summary: str = Field(
        default="No summary provided.",
        description="1-2 sentences explaining the overall verdict."
    )

# ==========================================
# 2. THE EVALUATOR
# ==========================================

class TechnicalsGroundingEval:
    """Evaluates if a TechnicalsAnalysis hallucinates data, patterns, or trends."""
    
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self.eval_name = "Technicals_GroundingEval"

    async def run(self, input_data: TechnicalsAnalysis) -> EvalResult:
        # 1. Isolate the ground truth evidence.
        # Based on your technicals.py, we have SMA-20, RSI-14, and a date envelope.
        evidence_dict = {
            "current_price": getattr(input_data, "current_price", None),
            "sma_20": getattr(input_data, "sma_20", None),
            "rsi_14": getattr(input_data, "rsi_14", None),
            "date_range": getattr(input_data, "date_range", {}),
            "as_of": getattr(input_data, "as_of", None)
        }
        
        # 2. Build the Multi-Layered Prompt
        system_prompt = """
You are evaluating whether a quantitative analyst's technical reasoning is faithfully grounded in the supporting data they were given.

You will receive:
1. SUPPORTING_TECHNICALS: a JSON object of technical indicators and time horizons. Treat this as ground truth.
2. REASONING: a paragraph of technical analysis written by the agent.

Your job: identify every technical claim in REASONING. Decompose each claim into one of three categories and evaluate it.

Categories & Grounding Rules:
1. NUMERIC_INTERPRETATION (e.g., "RSI of 72 suggests overbought")
- GROUNDED if: The exact number exists in SUPPORTING_TECHNICALS, AND the interpretation aligns with universally accepted technical analysis standard heuristics (e.g., RSI > 70 is overbought, < 30 is oversold).
- UNGROUNDED if: The number is wrong, or the interpretation is objectively false (e.g., calling RSI 20 "overbought").

2. PATTERN (e.g., "Death cross formed", "Price broke above SMA")
- GROUNDED if: The specific indicators required to form the pattern are explicitly present in the data, AND their mathematical relationship supports the pattern.
- UNGROUNDED if: The required indicators are missing entirely (e.g., claiming a "Death Cross" when only SMA-20 is provided in the data), or the math contradicts the pattern.

3. DIRECTIONAL / TEMPORAL (e.g., "Momentum is building", "Trend reversed")
- GROUNDED if: The data contains sufficient temporal history (e.g., an explicit date range spanning weeks/months) to prove the change over time.
- UNGROUNDED if: The data is only a single day's snapshot or lacks the historical context needed to prove a "trend".

Be strict. If the agent claims a pattern or trend but the provided data does not contain the specific fields to prove it, mark it UNGROUNDED and explain that the data was missing.

Return your verdict as a structured judgment with:
- claims: list of {claim_text, claim_type, verdict, explanation}
- overall_passed: true only if ALL claims are GROUNDED
- summary: 1-2 sentences explaining the verdict
"""
        
        user_prompt = f"""
            SUPPORTING_TECHNICALS:
            {json.dumps(evidence_dict, indent=2)}

            REASONING:
            {input_data.reasoning}

            Please evaluate.
        """

        # 3. Execute the API Wrapper
        judgment: TechnicalGroundingJudgment = await judge(
            client=self.client,
            system_prompt=system_prompt.strip(),
            user_prompt=user_prompt.strip(),
            response_schema=TechnicalGroundingJudgment
        )

        # 4. Map back to EvalResult
        total_claims = len(judgment.claims)
        if total_claims > 0:
            grounded_count = sum(1 for c in judgment.claims if c.verdict == "GROUNDED")
            score = grounded_count / total_claims
        else:
            score = 1.0

        return EvalResult(
            eval_name=self.eval_name,
            passed=judgment.overall_passed,
            score=score,
            ticker=getattr(input_data, 'ticker', None),
            details=judgment.model_dump() 
        )