"""
Pinned model registry — the single source of truth for the specialist models.

Model drift is a silent killer: upgrading a model changes the validated behavior AND
shifts the training-data contamination cutoff AND the pricing. Pinning every model ID,
its training-data cutoff, and its list price in one place makes a model change a
*deliberate edit here* — and the signal to re-run the validation suite
(productization.md 3.1). Treat editing this file like a deploy.

Downstream (budget pricing, temporal cutoffs, the specialists) reads from here, so
there's no second copy to drift. Each run records these IDs (runlog) for provenance.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str                  # Anthropic API model ID (a pinned snapshot)
    training_cutoff: str     # YYYY-MM-DD, training-DATA cutoff (conservative; for contamination)
    input_per_mtok: float    # USD per 1M input tokens (list price)
    output_per_mtok: float   # USD per 1M output tokens


# ROOT makes the judgments (fundamentals/technicals + sentiment/news synthesis);
# LEAF does the cheap sentiment/news map-reduce leaf step. Cutoffs/prices verified
# against the Claude models overview (see notes/productization.md 0.0).
ROOT = ModelSpec("claude-sonnet-4-6", "2026-01-31", 3.0, 15.0)
LEAF = ModelSpec("claude-haiku-4-5-20251001", "2025-07-31", 1.0, 5.0)

REGISTRY: dict[str, ModelSpec] = {ROOT.id: ROOT, LEAF.id: LEAF}

# The binding contamination boundary is the LATEST training cutoff among the models
# that make decisions — ROOT drives every judgment, so its cutoff governs.
DECISION_MODEL_CUTOFF = ROOT.training_cutoff


def price_table() -> dict[str, tuple[float, float]]:
    """{model_id: (input_$/Mtok, output_$/Mtok)} for the spend meter. Includes the
    dateless Haiku alias so either form prices correctly."""
    table = {m.id: (m.input_per_mtok, m.output_per_mtok) for m in REGISTRY.values()}
    table["claude-haiku-4-5"] = (LEAF.input_per_mtok, LEAF.output_per_mtok)  # alias
    return table


def pinned_ids() -> list[str]:
    return [ROOT.id, LEAF.id]
