import pytest

from src.backtest.score_all import signed_conviction
from src.schemas.signal import Decision, SpecialistSignal


def _sig(specialist: str, signal: str, conf: float) -> SpecialistSignal:
    return SpecialistSignal(
        specialist=specialist, ticker="X", signal=signal, confidence=conf,
        reasoning="x" * 60, as_of="2026-02-06",
    )


def _decision(direction: str, conf: float, signals: list[SpecialistSignal]) -> Decision:
    return Decision(
        ticker="X", direction=direction, confidence=conf, rationale="x" * 60,
        contributing_signals=signals, as_of="2026-02-06",
    )


def test_signed_conviction_is_continuous_pretthreshold_score():
    # Two bullish @0.8/0.6 + one bearish @0.4 -> (0.8 + 0.6 - 0.4)/3 = 0.333...
    sigs = [_sig("fundamentals", "BULLISH", 0.8),
            _sig("technicals", "BULLISH", 0.6),
            _sig("sentiment", "BEARISH", 0.4)]
    d = _decision("BUY", 0.5, sigs)
    assert signed_conviction(d) == pytest.approx((0.8 + 0.6 - 0.4) / 3)


def test_signed_conviction_nonzero_even_when_direction_is_hold():
    # Net score 0.2/2 = 0.1 -> below the 0.3 BUY threshold so synthesis says HOLD, but the
    # continuous conviction is a distinct +0.1 (the HOLD-collapse fix: not flattened to 0).
    sigs = [_sig("fundamentals", "BULLISH", 0.5),
            _sig("technicals", "BEARISH", 0.3)]
    d = _decision("HOLD", 0.0, sigs)
    assert signed_conviction(d) == pytest.approx((0.5 - 0.3) / 2)
    assert signed_conviction(d) != 0.0


def test_signed_conviction_no_signals_is_zero():
    assert signed_conviction(_decision("HOLD", 0.0, [])) == 0.0
