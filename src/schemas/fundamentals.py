from typing import Literal

from src.schemas.signal import SpecialistSignal


class FundamentalsAnalysis(SpecialistSignal):
    # The Literal "tags" this specific record
    specialist: Literal["fundamentals"]

    # Typed evidence fields replacing the generic dict
    pe_ratio: float | None = None
    profit_margin: float | None = None
    rev_growth_yoy: float
    debt_to_equity: float | None = None
