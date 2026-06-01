import pytest

from evals.fundamentals.consistency import ConsistencyEval
from src.schemas.fundamentals import FundamentalsAnalysis


def build_analysis(signal: str, rev_growth_yoy: float, **kwargs) -> FundamentalsAnalysis:
    defaults = {
        "specialist": "fundamentals",
        "ticker": "TEST",
        "confidence": 0.7,
        "reasoning": (
            "Test analysis for consistency eval calibration. "
            "This reasoning text is long enough to satisfy the min_length constraint on the field."
        ),
        "as_of": "2026-05-22",
    }
    defaults.update(kwargs)
    return FundamentalsAnalysis(
        signal=signal,
        rev_growth_yoy=rev_growth_yoy,
        **defaults,
    )


@pytest.fixture
def consistency_eval():
    return ConsistencyEval()


# ==========================================
# TESTS
# ==========================================

@pytest.mark.asyncio
async def test_consistency_bearish_metrics_bullish_signal(consistency_eval):
    """
    Planted-failure test: clear BEARISH conditions but BULLISH signal.

    rev_growth_yoy = -0.15 (declining revenue) + profit_margin = 0.05 (thin) →
    threshold rules imply BEARISH. Planted contradiction: signal = "BULLISH".
    """
    analysis = build_analysis(
        signal="BULLISH",
        rev_growth_yoy=-0.15,
        profit_margin=0.05,
        pe_ratio=45.0,
        debt_to_equity=0.8,
    )

    result = await consistency_eval.run(analysis)

    assert result.passed is False, "Eval should fail — declining revenue + thin margin contradicts BULLISH."
    assert result.details.get("verdict") == "INCONSISTENT"
    assert result.details.get("expected_signal") == "BEARISH"
    assert result.details.get("actual_signal") == "BULLISH"


@pytest.mark.asyncio
async def test_consistency_bullish_metrics_bullish_signal(consistency_eval):
    """
    Happy path: strong BULLISH conditions and BULLISH signal — should pass.

    rev_growth_yoy = 0.20 + profit_margin = 0.25 + pe_ratio = 18 + low debt →
    threshold rules imply BULLISH. Signal = "BULLISH" is consistent.
    """
    analysis = build_analysis(
        signal="BULLISH",
        rev_growth_yoy=0.20,
        profit_margin=0.25,
        pe_ratio=18.0,
        debt_to_equity=0.4,
    )

    result = await consistency_eval.run(analysis)

    assert result.passed is True, "Eval should pass — strong metrics are consistent with BULLISH."
    assert result.details.get("verdict") == "CONSISTENT"


@pytest.mark.asyncio
async def test_consistency_ambiguous_metrics_any_signal(consistency_eval):
    """
    Ambiguous case: mixed signals — any signal direction should pass.

    High growth but also high P/E and thin margin — rules produce no clear expectation.
    NEUTRAL signal should pass; specialist has legitimate discretion here.
    """
    analysis = build_analysis(
        signal="NEUTRAL",
        rev_growth_yoy=0.18,   # strong growth
        profit_margin=0.08,    # thin margin
        pe_ratio=55.0,         # elevated P/E
        debt_to_equity=1.5,
    )

    result = await consistency_eval.run(analysis)

    assert result.passed is True, "Eval should pass — ambiguous metrics give specialist discretion."
    assert result.details.get("verdict") == "AMBIGUOUS"
