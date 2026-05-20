from typing import Literal, Dict, Optional

from src.schemas.signal import SpecialistSignal

class TechnicalsAnalysis(SpecialistSignal):
    # The Literal "tags" this specific record
    specialist: Literal["technicals"]

    # Typed evidence fields replacing the generic dict.
    # These MUST match the keys from TechnicalsSnapshot exactly!
    current_price: float | None = None
    sma_20: Optional[float] = None
    rsi_14: Optional[float] = None
    date_range: Optional[Dict[str, str]] = None