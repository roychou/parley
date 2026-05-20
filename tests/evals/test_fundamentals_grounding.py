import pytest
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from evals.fundamentals import GroundingEval
from src.schemas.fundamentals import FundamentalsAnalysis

load_dotenv()

# ==========================================
# FIXTURES
# ==========================================

@pytest.fixture
def eval_client():
    """Provides a shared Anthropic client for the tests."""
    return AsyncAnthropic()

@pytest.fixture
def grounding_eval(eval_client):
    """Provides an initialized GroundingEval instance."""
    return GroundingEval(eval_client)

def build_dummy_analysis(reasoning: str, **kwargs) -> FundamentalsAnalysis:
    """Helper to build a valid FundamentalsAnalysis with injected reasoning."""
    default_data = {
        "specialist": "fundamentals",
        "ticker": "TEST",
        "as_of": "2026-05-12",
        "signal": "NEUTRAL",
        "confidence": 0.5,
        "pe_ratio": 15.0,
        "profit_margin": 0.20,
        "rev_growth_yoy": 0.10,
        "debt_to_equity": 0.5
    }
    default_data.update(kwargs)
    default_data["reasoning"] = reasoning
    return FundamentalsAnalysis(**default_data)

# ==========================================
# TESTS
# ==========================================

@pytest.mark.asyncio
async def test_grounding_numeric_contradiction(grounding_eval):
    """
    Test that the judge catches a direct numerical hallucination.
    The data says P/E is 12.0, but the reasoning claims it is 50.
    """
    analysis = build_dummy_analysis(
        pe_ratio=12.0,
        reasoning="The company's P/E of 50 is highly elevated, suggesting the stock is heavily overvalued right now."
    )
    
    result = await grounding_eval.run(analysis)
    
    # 1. Assert the overall eval failed
    assert result.passed is False, "Eval should have failed due to the planted P/E hallucination."
    
    # 2. Assert the specific claim was caught
    claims = result.details.get("claims", [])
    assert len(claims) > 0, "Judge failed to extract any claims."
    
    # Check if any claim correctly identified the P/E hallucination as UNGROUNDED
    caught_hallucination = False
    for claim in claims:
        if claim["verdict"] == "UNGROUNDED" and ("P/E" in claim["claim_text"] or "50" in claim["claim_text"]):
            caught_hallucination = True
            break
            
    assert caught_hallucination, f"Judge failed to explicitly flag the P/E hallucination. Claims: {claims}"

@pytest.mark.asyncio
async def test_grounding_directional_contradiction(grounding_eval):
    """
    Test that the judge catches an absurd qualitative characterization.
    The data says profit margin is 35% (very strong), but the reasoning calls it weak.
    """
    analysis = build_dummy_analysis(
        profit_margin=0.35, # 35% margin
        reasoning="With profit margins being weak and the company struggling to generate cash, the outlook is poor."
    )
    
    result = await grounding_eval.run(analysis)
    
    # 1. Assert the overall eval failed
    assert result.passed is False, "Eval should have failed due to the planted margin hallucination."
    
    # 2. Assert the specific claim was caught
    claims = result.details.get("claims", [])
    assert len(claims) > 0, "Judge failed to extract any claims."
    
    # Check if any claim correctly identified the margin characterization as UNGROUNDED
    caught_hallucination = False
    for claim in claims:
        if claim["verdict"] == "UNGROUNDED" and ("margin" in claim["claim_text"].lower()):
            caught_hallucination = True
            break
            
    assert caught_hallucination, f"Judge failed to explicitly flag the directional margin hallucination. Claims: {claims}"