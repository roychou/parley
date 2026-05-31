from typing import Literal

from src.schemas.signal import SpecialistSignal


class NewsAnalysis(SpecialistSignal):
    """News specialist output: judgment from recent news flow about the company.

    A FORWARD-ONLY specialist — it reads news as of the decision date, which is
    point-in-time and uncontaminated only when that date is live/after the model's
    training cutoff. It is deliberately NOT used in the historical backtest (where
    news is both hard to snapshot point-in-time and already in the model's training
    data). Synthesis consumes only the base SpecialistSignal fields; the rest is
    typed evidence for the audit trail."""

    specialist: Literal["news"]

    # Evidence
    overall_tone: str | None = None        # e.g. "positive" / "negative" / "mixed"
    key_events: list[str] = []             # concrete catalysts in the window (M&A, guidance, legal…)
    n_articles: int = 0                    # how many articles informed this signal
    lookback_days: int = 0                 # the news window analyzed
    top_headlines: list[str] = []          # a few representative headlines (audit trail)
