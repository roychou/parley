from typing import Literal

from src.schemas.signal import SpecialistSignal


class SentimentAnalysis(SpecialistSignal):
    """Sentiment specialist output: judgment from the filing *narrative* (MD&A,
    Risk Factors) — management's own words — combining current tone with material
    changes vs the prior filing. Synthesis consumes only the base SpecialistSignal
    fields; the rest is typed evidence for the audit trail."""

    specialist: Literal["sentiment"]

    # Evidence
    tone: str | None = None                      # e.g. "optimistic" / "cautious" / "mixed"
    key_themes: list[str] = []                   # salient topics in the current filing
    notable_changes: list[str] = []              # shifts vs the prior same-form filing
    source_form: str | None = None               # "10-Q" / "10-K"
    filed: str | None = None                     # filing date of the current document
