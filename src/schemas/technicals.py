from typing import Literal

from src.schemas.signal import SpecialistSignal


class TechnicalsAnalysis(SpecialistSignal):
    # The Literal "tags" this specific record
    specialist: Literal["technicals"]

    # Typed evidence fields replacing the generic dict.
    # These MUST match the keys from TechnicalsSnapshot exactly!
    current_price: float | None = None
    sma_20: float | None = None
    rsi_14: float | None = None
    date_range: dict[str, str] | None = None
