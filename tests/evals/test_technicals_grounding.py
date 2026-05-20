import pytest
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from evals.technicals.grounding import TechnicalsGroundingEval
from src.schemas.technicals import TechnicalsAnalysis

load_dotenv()

# ==========================================
# FIXTURES
# ==========================================

@pytest.fixture
def eval_client():
    """Provides a shared Anthropic client for the tests."""
    return AsyncAnthropic()

@pytest.fixture
def technicals_eval(eval_client):
    """Provides an initialized TechnicalsGroundingEval instance."""
    return TechnicalsGroundingEval(eval_client)

def build_dummy_tech_analysis(reasoning: str, **kwargs) -> TechnicalsAnalysis:
    """Helper to build a valid TechnicalsAnalysis with injected reasoning."""
    default_data = {
        "specialist": "technicals",
        "ticker": "TEST",
        "as_of": "2026-05-12",
        "signal": "NEUTRAL",
        "confidence": 0.5,
        "current_price": 150.0,
        "sma_20": 145.0,
        "rsi_14": 50.0,
        "date_range": {"start": "2026-04-12", "end": "2026-05-12"}
    }
    default_data.update(kwargs)
    default_data["reasoning"] = reasoning
    return TechnicalsAnalysis(**default_data)

# ==========================================
# TESTS
# ==========================================

@pytest.mark.asyncio
async def test_tech_grounding_numeric_interpretation_inversion(technicals_eval):
    """
    CASE 1: NUMERIC_INTERPRETATION
    Test that the judge catches a correct number with a wildly incorrect interpretation.
    Data: RSI is 25 (oversold). Reasoning: Claims 25 is overbought.
    """
    analysis = build_dummy_tech_analysis(
        rsi_14=25.0,
        reasoning="An RSI of 25 suggests strong overbought conditions, signaling a sell."
    )
    
    result = await technicals_eval.run(analysis)
    
    assert result.passed is False, "Eval should have failed due to inverted RSI interpretation."
    
    claims = result.details.get("claims", [])
    assert len(claims) > 0, "Judge failed to extract any claims."
    
    caught = False
    for claim in claims:
        if (claim["verdict"] == "UNGROUNDED" and 
            "25" in claim["claim_text"]):
            caught = True
            break
            
    assert caught, f"Judge failed to flag the inverted numeric interpretation. Claims: {claims}"

@pytest.mark.asyncio
async def test_tech_grounding_pattern_missing_prereqs(technicals_eval):
    """
    CASE 2: PATTERN
    Test that the judge catches a pattern claim when the required indicators do not exist.
    Data: Only has SMA-20 and Price. Reasoning: Claims a 50/200-day Death Cross.
    """
    analysis = build_dummy_tech_analysis(
        sma_20=145.0,
        current_price=150.0,
        reasoning="A death cross formed as the 50-day SMA crossed below the 200-day SMA, signaling bearish momentum."
    )
    
    result = await technicals_eval.run(analysis)
    
    assert result.passed is False, "Eval should have failed due to missing pattern prerequisites."
    
    claims = result.details.get("claims", [])
    assert len(claims) > 0, "Judge failed to extract any claims."
    
    caught = False
    for claim in claims:
        if claim["verdict"] == "UNGROUNDED":
            text_blob = (claim["claim_text"] + " " + claim.get("explanation", "")).lower()
            if "death cross" in text_blob or ("50" in text_blob and "200" in text_blob) or "sma-50" in text_blob or "sma-200" in text_blob:
                caught = True
                break

    assert caught, f"Judge failed to flag the fabricated pattern prerequisites. Claims: {claims}"

@pytest.mark.asyncio
async def test_tech_grounding_directional_without_history(technicals_eval):
    """
    CASE 3: DIRECTIONAL / TEMPORAL
    Test that the judge catches a long-term trend claim when given only point-in-time data.
    Data: Empty date range (snapshot). Reasoning: Claims momentum over three weeks.
    """
    analysis = build_dummy_tech_analysis(
        date_range={}, # Purposely blank out the history
        rsi_14=55.0,
        current_price=150.0,
        sma_20=148.0,
        reasoning="Momentum has been steadily building over the past three weeks, with the trend reversing from bearish to bullish."
    )
    
    result = await technicals_eval.run(analysis)
    
    assert result.passed is False, "Eval should have failed due to lack of temporal data for the trend claim."
    
    claims = result.details.get("claims", [])
    assert len(claims) > 0, "Judge failed to extract any claims."
    
    caught = False
    for claim in claims:
        if (claim["verdict"] == "UNGROUNDED" and 
            "three weeks" in claim["claim_text"].lower()):
            caught = True
            break
            
    assert caught, f"Judge failed to flag the unsupported temporal claim. Claims: {claims}"