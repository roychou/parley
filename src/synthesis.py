import logging
from dataclasses import dataclass

from src.schemas import Decision, SpecialistSignal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SIGNAL_TO_SCORE = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}


@dataclass(frozen=True)
class SynthesisConfig:
    """Knobs for the confidence-weighted vote. Thresholds default to the historical
    |score| > 0.3; exposed so they're tunable once the forward record gives evidence."""
    buy_threshold: float = 0.3
    sell_threshold: float = 0.3


def synthesize(
    ticker: str,
    signals: list[SpecialistSignal],
    as_of: str,
    config: SynthesisConfig = SynthesisConfig(),
) -> Decision:
    """Confidence-weighted vote across specialist signals, with agreement-aware
    confidence.

    Direction is the net directional score (mean of confidence × ±1) vs the BUY/SELL
    thresholds — unchanged from the original. Confidence, however, is now
    |score| × agreement, where agreement is the fraction of *directional* specialists
    pointing the way the score does. So a 2-bullish-1-bearish split sizes smaller than
    a unanimous read of the same net score: position size reflects disagreement, not
    just net conviction. (A future v2 may replace the linear vote with an LLM that
    reasons over the analyses; this stays deterministic, cheap, and reproducible.)
    """
    if not signals:
        raise ValueError("synthesize requires at least one signal")

    score = sum(s.confidence * SIGNAL_TO_SCORE[s.signal] for s in signals) / len(signals)
    direction = (
        "BUY" if score > config.buy_threshold
        else "SELL" if score < -config.sell_threshold
        else "HOLD"
    )

    # Agreement: of the specialists with a directional view, the fraction aligned with
    # the net score. 1.0 = unanimous (or a single voice); < 1.0 = genuine disagreement.
    score_sign = 1 if score > 0 else -1 if score < 0 else 0
    directional = [s for s in signals if SIGNAL_TO_SCORE[s.signal] != 0]
    if directional and score_sign != 0:
        aligned = sum(1 for s in directional if SIGNAL_TO_SCORE[s.signal] == score_sign)
        agreement = aligned / len(directional)
    else:
        agreement = 0.0  # all-neutral or perfectly balanced -> no conviction
    confidence = min(abs(score) * agreement, 1.0)

    n_bull = sum(1 for s in signals if s.signal == "BULLISH")
    n_bear = sum(1 for s in signals if s.signal == "BEARISH")
    if len(directional) <= 1:
        note = ""
    elif agreement == 1.0:
        note = " All directional specialists agree."
    else:
        note = (
            f" Specialists disagree ({n_bull} bullish vs {n_bear} bearish) — "
            f"confidence reduced to {confidence:.2f}."
        )
    rationale = (
        f"{len(signals)} specialists: "
        + ", ".join(f"{s.specialist}={s.signal}@{s.confidence:.2f}" for s in signals)
        + f". Weighted score {score:+.2f}.{note}"
    )

    return Decision(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        rationale=rationale,
        contributing_signals=signals,
        as_of=as_of,
    )
