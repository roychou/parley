"""Pinned model registry — single-source-of-truth invariants."""
from src.models import DECISION_MODEL_CUTOFF, LEAF, REGISTRY, ROOT, pinned_ids, price_table


def test_decision_cutoff_is_root_training_cutoff():
    # ROOT makes the judgments, so its (later) training cutoff is the binding boundary.
    assert DECISION_MODEL_CUTOFF == ROOT.training_cutoff
    assert ROOT.training_cutoff >= LEAF.training_cutoff


def test_price_table_covers_registry_and_haiku_alias():
    table = price_table()
    for m in REGISTRY.values():
        assert table[m.id] == (m.input_per_mtok, m.output_per_mtok)
    # dateless Haiku alias prices the same
    assert table["claude-haiku-4-5"] == (LEAF.input_per_mtok, LEAF.output_per_mtok)


def test_pinned_ids_are_root_and_leaf():
    assert pinned_ids() == [ROOT.id, LEAF.id]
