from typing import Literal

from src.schemas.signal import SpecialistSignal


class TechnicalsAnalysis(SpecialistSignal):
    # The Literal "tags" this specific record
    specialist: Literal["technicals"]

    # Typed evidence fields replacing the generic dict
    sma_50: float | None = None
    rsi: float | None = None
    macd_histogram: float | None = None
