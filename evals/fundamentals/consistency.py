"""
Consistency eval for FundamentalsAnalysis.

Checks whether the signal direction is consistent with explicit threshold rules.
Unlike grounding (which checks the reasoning), consistency checks the output signal
against deterministic thresholds. No LLM judge — the rules are computable.

The specialist has discretion when metrics conflict, so only unambiguous cases are
checked. NEUTRAL is always acceptable; only clear contradictions (BULLISH rules →
BEARISH signal, or BEARISH rules → BULLISH signal) are flagged.

Rules are derived from the fundamentals specialist prompt thresholds and live here,
not in the specialist. They can evolve independently.
"""
from src.evals.base import EvalResult
from src.schemas.fundamentals import FundamentalsAnalysis

# ==========================================
# 1. THRESHOLD RULES
# ==========================================

def _expected_signal(analysis: FundamentalsAnalysis) -> str | None:
    """Returns the signal implied by threshold rules, or None if ambiguous.

    None means the rules produce no clear expectation — the specialist has
    legitimate discretion and any signal is acceptable.
    """
    pm = analysis.profit_margin   # > 0.20 is strong (per specialist prompt)
    pe = analysis.pe_ratio        # > 40 is high, < 15 is low
    rg = analysis.rev_growth_yoy  # > 0.15 is strong, < 0.0 is bearish
    de = analysis.debt_to_equity  # > 2.0 is concerning

    bullish = (
        rg > 0.15
        and (pm is not None and pm > 0.20)
        and (pe is None or pe < 40)
        and (de is None or de < 2.0)
    )

    # Both conditions negative: declining revenue AND thin margins
    bearish = rg < 0.0 and (pm is not None and pm < 0.10)

    if bullish and not bearish:
        return "BULLISH"
    if bearish and not bullish:
        return "BEARISH"
    return None  # Ambiguous — specialist has discretion


# ==========================================
# 2. THE EVALUATOR
# ==========================================

class ConsistencyEval:
    """Checks if a FundamentalsAnalysis signal contradicts its supporting data."""

    def __init__(self):
        self.eval_name = "Fundamentals_ConsistencyEval"

    async def run(self, analysis: FundamentalsAnalysis) -> EvalResult:
        expected = _expected_signal(analysis)

        if expected is None:
            return EvalResult(
                eval_name=self.eval_name,
                passed=True,
                score=1.0,
                ticker=analysis.ticker,
                details={
                    "verdict": "AMBIGUOUS",
                    "actual_signal": analysis.signal,
                    "explanation": (
                        "Threshold rules produce no unambiguous expected signal. "
                        "Specialist discretion applies; any signal is acceptable."
                    ),
                    "metrics": _metrics_dict(analysis),
                },
            )

        # Contradiction: rules say BULLISH but signal is BEARISH, or vice versa.
        # NEUTRAL is acceptable in either direction — the specialist may have
        # legitimate reasons to hedge even when rules are unambiguous.
        contradiction = (
            (expected == "BULLISH" and analysis.signal == "BEARISH")
            or (expected == "BEARISH" and analysis.signal == "BULLISH")
        )

        return EvalResult(
            eval_name=self.eval_name,
            passed=not contradiction,
            score=0.0 if contradiction else 1.0,
            ticker=analysis.ticker,
            details={
                "verdict": "INCONSISTENT" if contradiction else "CONSISTENT",
                "expected_signal": expected,
                "actual_signal": analysis.signal,
                "explanation": (
                    f"Threshold rules imply {expected}; specialist returned {analysis.signal}."
                    if contradiction else
                    f"Signal '{analysis.signal}' is consistent with threshold rules implying {expected}."
                ),
                "metrics": _metrics_dict(analysis),
            },
        )


def _metrics_dict(analysis: FundamentalsAnalysis) -> dict:
    return {
        "rev_growth_yoy": analysis.rev_growth_yoy,
        "profit_margin": analysis.profit_margin,
        "pe_ratio": analysis.pe_ratio,
        "debt_to_equity": analysis.debt_to_equity,
    }
