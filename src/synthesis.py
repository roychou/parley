import logging

from src.schemas import SpecialistSignal, Decision

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SIGNAL_TO_SCORE = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}


def synthesize(ticker: str, signals: list[SpecialistSignal], as_of: str) -> Decision:
    """Confidence-weighted vote across specialist signals.

    Stub implementation: BUY/SELL thresholds at |score| > 0.3.
    Real synthesis (Release 2+) will handle per-specialist weighting,
    disagreement detection, and calibrated confidence.
    """
    if not signals:
        raise ValueError("synthesize requires at least one signal")

    score = sum(s.confidence * SIGNAL_TO_SCORE[s.signal] for s in signals) / len(signals)

    direction = "BUY" if score > 0.3 else "SELL" if score < -0.3 else "HOLD"

    rationale = (
        f"{len(signals)} specialists: "
        + ", ".join(f"{s.specialist}={s.signal}@{s.confidence:.2f}" for s in signals)
        + f". Weighted score: {score:+.2f}."
    )

    return Decision(
        ticker=ticker,
        direction=direction,
        confidence=min(abs(score), 1.0),
        rationale=rationale,
        contributing_signals=signals,
        as_of=as_of,
    )
