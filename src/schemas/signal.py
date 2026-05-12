from pydantic import BaseModel, Field
from typing import Literal

SignalDirection = Literal["BULLISH", "BEARISH", "NEUTRAL"]


class SpecialistSignal(BaseModel):
    """
    Base contract every specialist output conforms to.
    Synthesis logic relies ONLY on these fields.
    """

    specialist: str  # Will be overridden by strict Literals in subclasses
    ticker: str
    signal: SignalDirection
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=50)
    as_of: str  # YYYY-MM-DD


DecisionDirection = Literal["BUY", "HOLD", "SELL"]


# Real confidence requires either ensemble disagreement analysis or model-based synthesis, both out of scope for now.
class Decision(BaseModel):
    """Final synthesized output of the supervisor for a single ticker."""

    ticker: str
    direction: DecisionDirection
    confidence: float = Field(ge=0.0, le=1.0)  # TODO: confidence calibration in synthesis v2
    rationale: str = Field(min_length=50)
    contributing_signals: list[SpecialistSignal]
    as_of: str  # YYYY-MM-DD, the decision date
