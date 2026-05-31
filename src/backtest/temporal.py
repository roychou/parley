"""
Temporal validity — separating training-contaminated decision dates from clean ones.

The foundational flaw of an LLM-driven backtest (productization.md Phase 0.0): a
decision the model makes about a date *inside its training window* is contaminated by
the model's memory of what those tickers did. Point-in-time data hygiene does not fix
it — the leakage is in the weights. The only clean evaluation is decision dates strictly
*after* the specialist models' training cutoff (plus forward paper trading).

This module makes the split explicit so the runner can:
- report how many decision dates are clean vs. contaminated, and
- optionally restrict a run to the clean (post-cutoff) window.

The cutoff is **operator-supplied and must be verified per model** — there is no
reliable programmatic source, and guessing wrong silently re-contaminates the result.
When it's unset, every result is treated as engineering validation, not edge evidence.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def partition_by_cutoff(
    dates: list[str], model_cutoff: str | None
) -> tuple[list[str], list[str]]:
    """Split sorted decision dates into (contaminated, clean) around the model cutoff.

    Contaminated = on or before the cutoff (in the training window); clean = strictly
    after. With no cutoff, all dates are treated as contaminated (we can't claim clean)."""
    ordered = sorted(dates)
    if not model_cutoff:
        return ordered, []
    contaminated = [d for d in ordered if d <= model_cutoff]
    clean = [d for d in ordered if d > model_cutoff]
    return contaminated, clean


def report_and_filter(
    dates: list[str], model_cutoff: str | None, clean_only: bool
) -> list[str]:
    """Log the contamination split and return the dates to actually run.

    clean_only restricts to the post-cutoff window (raises if that leaves nothing).
    Otherwise the full schedule runs, but with a loud caveat when any date is
    contaminated — those results are engineering validation, not an edge claim."""
    contaminated, clean = partition_by_cutoff(dates, model_cutoff)

    if not model_cutoff:
        logger.warning(
            "TEMPORAL VALIDITY: model cutoff not set (--model-cutoff). Cannot certify any "
            "date as post-training-cutoff — treat ALL results as engineering validation, "
            "not edge evidence. See notes/productization.md Phase 0.0."
        )
        return sorted(dates)

    logger.info(
        f"TEMPORAL VALIDITY: cutoff {model_cutoff} -> {len(clean)} clean (post-cutoff), "
        f"{len(contaminated)} contaminated (in training window)."
    )
    if clean_only:
        if not clean:
            raise ValueError(
                f"--clean-only: no decision dates after the model cutoff {model_cutoff}. "
                "The clean window is empty — widen the dates or lower the cutoff (if valid)."
            )
        logger.info(f"--clean-only: running the {len(clean)} post-cutoff dates only.")
        return clean

    if contaminated:
        logger.warning(
            f"{len(contaminated)} of {len(dates)} decision dates are in the model's training "
            "window — those decisions are contaminated by memory. The blended result is NOT "
            "an edge claim. Use --clean-only for a temporally valid (if smaller) run."
        )
    return sorted(dates)
