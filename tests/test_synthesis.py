"""Synthesis = confidence-weighted vote with agreement-aware confidence."""
from src.schemas import SpecialistSignal
from src.synthesis import synthesize


def _sig(specialist: str, signal: str, conf: float) -> SpecialistSignal:
    return SpecialistSignal(specialist=specialist, ticker="TST", signal=signal,
                            confidence=conf, reasoning="x" * 60, as_of="2026-05-01")


def test_unanimous_bullish_full_confidence():
    d = synthesize("TST", [_sig("fundamentals", "BULLISH", 0.8),
                           _sig("technicals", "BULLISH", 0.6)], "2026-05-01")
    assert d.direction == "BUY"
    assert abs(d.confidence - 0.7) < 1e-9          # score 0.7 × agreement 1.0
    assert "agree" in d.rationale.lower()


def test_disagreement_reduces_confidence_and_is_noted():
    sigs = [_sig("fundamentals", "BULLISH", 0.9), _sig("technicals", "BULLISH", 0.9),
            _sig("sentiment", "BEARISH", 0.8)]
    d = synthesize("TST", sigs, "2026-05-01")
    score = (0.9 + 0.9 - 0.8) / 3                  # ≈ 0.333 -> BUY
    assert d.direction == "BUY"
    assert d.confidence < abs(score)               # agreement 2/3 shrinks confidence
    assert abs(d.confidence - score * (2 / 3)) < 1e-9
    assert "disagree" in d.rationale.lower()       # disagreement acknowledged


def test_balanced_conflict_is_hold_zero_confidence():
    d = synthesize("TST", [_sig("fundamentals", "BULLISH", 0.8),
                           _sig("technicals", "BEARISH", 0.8)], "2026-05-01")
    assert d.direction == "HOLD"
    assert d.confidence == 0.0


def test_single_directional_voice_keeps_full_confidence():
    d = synthesize("TST", [_sig("fundamentals", "BULLISH", 0.5)], "2026-05-01")
    assert d.direction == "BUY" and abs(d.confidence - 0.5) < 1e-9


def test_all_neutral_is_hold():
    d = synthesize("TST", [_sig("fundamentals", "NEUTRAL", 0.6),
                           _sig("technicals", "NEUTRAL", 0.4)], "2026-05-01")
    assert d.direction == "HOLD" and d.confidence == 0.0
