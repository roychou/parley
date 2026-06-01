import pytest
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from evals.synthesis.grounding import SynthesisGroundingEval
from src.schemas.fundamentals import FundamentalsAnalysis
from src.schemas.signal import Decision
from src.schemas.technicals import TechnicalsAnalysis

load_dotenv()

# ==========================================
# FIXTURES
# ==========================================

@pytest.fixture
def eval_client():
    return AsyncAnthropic()

@pytest.fixture
def synthesis_eval(eval_client):
    return SynthesisGroundingEval(eval_client)

# ==========================================
# TESTS
# ==========================================

@pytest.mark.asyncio
async def test_synthesis_disagreement_blindness(synthesis_eval):
    """
    Planted-failure test: disagreement blindness.

    Fundamentals=BULLISH@0.9, Technicals=BEARISH@0.8.
    Weighted score ≈ (0.9 - 0.8) / 2 = 0.05 → direction should be HOLD, confidence should be low.
    Planted contradiction: BUY@0.85 with a rationale that ignores the BEARISH technicals signal.

    The judge must catch that:
    1. BUY is the wrong direction for near-balanced signals.
    2. High confidence is unjustified when specialists conflict.
    3. The rationale ignores the technicals specialist.
    """
    signals = [
        FundamentalsAnalysis(
            specialist="fundamentals",
            ticker="AAPL",
            signal="BULLISH",
            confidence=0.9,
            reasoning=(
                "Strong fundamentals across the board: P/E of 18 is reasonable, profit margins "
                "at 25% are robust, and revenue growth of 12% year-over-year is solid. "
                "The balance sheet is clean with debt-to-equity below 0.5."
            ),
            as_of="2026-05-21",
            rev_growth_yoy=0.12,
            pe_ratio=18.0,
            profit_margin=0.25,
            debt_to_equity=0.45,
        ),
        TechnicalsAnalysis(
            specialist="technicals",
            ticker="AAPL",
            signal="BEARISH",
            confidence=0.8,
            reasoning=(
                "Technical picture is deteriorating. RSI of 72 signals overbought conditions. "
                "Price at 185.0 is below SMA-20 of 190.0, indicating a short-term bearish "
                "reversal is already underway."
            ),
            as_of="2026-05-21",
            current_price=185.0,
            sma_20=190.0,
            rsi_14=72.0,
            date_range={"start": "2026-04-21", "end": "2026-05-21"},
        ),
    ]

    # Planted contradiction: BUY@0.85 despite near-equal opposing signals
    # Rationale ignores technicals entirely
    bad_decision = Decision(
        ticker="AAPL",
        direction="BUY",
        confidence=0.85,
        rationale=(
            "Strong bullish fundamentals with an excellent P/E ratio of 18, healthy profit margins "
            "of 25%, and solid revenue growth of 12% year-over-year clearly indicate this is a "
            "strong BUY opportunity with high conviction."
        ),
        contributing_signals=signals,
        as_of="2026-05-21",
    )

    result = await synthesis_eval.run(bad_decision)

    # Overall eval must fail
    assert result.passed is False, (
        "Eval should fail — BUY@0.85 despite BULLISH@0.9 vs BEARISH@0.8 is disagreement blindness."
    )

    # Judge must flag disagreement as not handled
    assert result.details.get("disagreement_handled") is False, (
        "Judge should flag that specialist disagreement was not handled correctly."
    )

    # Assert on evidence-of-detection: judge must cite the conflict
    disagreement_text = result.details.get("disagreement_reasoning", "").lower()
    assert any(kw in disagreement_text for kw in ["disagree", "conflict", "bearish", "technicals"]), (
        f"Judge should cite the specialist conflict in disagreement_reasoning. Got: {disagreement_text}"
    )
