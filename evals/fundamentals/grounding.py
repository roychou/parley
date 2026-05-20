"""
src/evals/fundamentals/grounding.py holds GroundingEval. It implements the Eval protocol. Its run method:

Takes a FundamentalsAnalysis (the specialist's output) as input.
Builds the judge prompt from the analysis's reasoning and supporting_fundamentals.
Calls judge(...) with a GroundingJudgment Pydantic schema.
Wraps the result in an EvalResult and returns it.
"""

import json
from typing import Any, List, Literal
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic

from src.evals.base import EvalProtocol, EvalResult
from src.evals.judge import judge
from src.schemas.fundamentals import FundamentalsAnalysis

# ==========================================
# 1. THE JUDGMENT SCHEMAS
# ==========================================

class ClaimVerdict(BaseModel):
    """Evaluates a single factual claim extracted from the reasoning."""
    claim_text: str = Field(description="The specific factual claim extracted from the reasoning.")
    verdict: Literal["GROUNDED", "UNGROUNDED"]
    explanation: str = Field(description="Why the claim is grounded or ungrounded based on the data.")

class GroundingJudgment(BaseModel):
    """The strict format we expect the LLM Judge to return."""
    claims: List[ClaimVerdict]
    overall_passed: bool = Field(description="True only if ALL claims are GROUNDED.")
    summary: str = Field(description="1-2 sentences explaining the overall verdict.")

# ==========================================
# 2. THE EVALUATOR
# ==========================================

class GroundingEval:
    """Evaluates if a FundamentalsAnalysis hallucinates or misrepresents data."""
    
    def __init__(self, client: AsyncAnthropic):
        self.client = client
        self.eval_name = "Fundamentals_GroundingEval"

    async def run(self, input_data: FundamentalsAnalysis) -> EvalResult:
        # 1. Isolate the ground truth evidence
        evidence_dict = {
            "pe_ratio": input_data.pe_ratio,
            "profit_margin": input_data.profit_margin,
            "rev_growth_yoy": input_data.rev_growth_yoy,
            "debt_to_equity": input_data.debt_to_equity,
            "as_of": input_data.as_of
        }
        
        # 2. Build the Prompts exactly as specified
        system_prompt = """
            You are evaluating whether a financial analyst's reasoning is faithfully
            grounded in the supporting data they were given.

            You will receive:
            1. SUPPORTING_FUNDAMENTALS: a JSON object of financial metrics. This is
            ground truth. Treat it as authoritative.
            2. REASONING: a paragraph of analysis written by the analyst.

            Your job: identify every factual claim in REASONING that references a
            specific metric, value, or quantitative comparison. For each claim,
            determine whether SUPPORTING_FUNDAMENTALS supports it.

            A claim is GROUNDED if:
            - The metric exists in SUPPORTING_FUNDAMENTALS, AND
            - The value cited matches (within reasonable rounding — e.g. "around 25"
            is fine for an actual value of 24.7), AND
            - Any comparison or characterization (e.g. "elevated", "low", "growing")
            is defensible given the data.

            A claim is UNGROUNDED if:
            - The metric is not in SUPPORTING_FUNDAMENTALS, OR
            - The value cited contradicts the data, OR
            - The characterization is unsupported (e.g. calling a P/E of 12 "elevated").

            Qualitative statements that don't reference specific data ("the company
            operates in a competitive market") are not factual claims and should be
            ignored.

            Be strict. If you are unsure whether a claim is grounded, mark it
            UNGROUNDED and explain why.

            Return your verdict as a structured judgment with:
            - claims: list of {claim_text, verdict, explanation}
            - overall_passed: true only if ALL claims are GROUNDED
            - summary: 1-2 sentences explaining the verdict
        """
        
        user_prompt = f"""
            SUPPORTING_FUNDAMENTALS:
            {json.dumps(evidence_dict, indent=2)}

            REASONING:
            {input_data.reasoning}

            Please evaluate.
        """

        # 3. Execute the API Wrapper
        judgment: GroundingJudgment = await judge(
            client=self.client,
            system_prompt=system_prompt.strip(),
            user_prompt=user_prompt.strip(),
            response_schema=GroundingJudgment
        )

        # 4. Map back to the generic EvalResult contract
        # We calculate a fractional score based on the ratio of grounded claims, 
        # defaulting to 1.0 if there are no explicit data claims to grade.
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