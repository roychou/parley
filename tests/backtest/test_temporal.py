"""Temporal-validity gate: contamination split + clean-only filtering."""
import pytest

from src.backtest.temporal import partition_by_cutoff, report_and_filter

_DATES = ["2025-01-15", "2025-06-01", "2025-12-01", "2026-03-01", "2026-05-01"]


def test_partition_splits_on_cutoff():
    contaminated, clean = partition_by_cutoff(_DATES, "2025-12-31")
    assert contaminated == ["2025-01-15", "2025-06-01", "2025-12-01"]  # <= cutoff
    assert clean == ["2026-03-01", "2026-05-01"]                       # strictly after


def test_no_cutoff_treats_all_as_contaminated():
    contaminated, clean = partition_by_cutoff(_DATES, None)
    assert contaminated == sorted(_DATES)
    assert clean == []


def test_report_and_filter_clean_only_restricts():
    out = report_and_filter(_DATES, model_cutoff="2025-12-31", clean_only=True)
    assert out == ["2026-03-01", "2026-05-01"]


def test_report_and_filter_full_run_keeps_all():
    out = report_and_filter(_DATES, model_cutoff="2025-12-31", clean_only=False)
    assert out == sorted(_DATES)


def test_clean_only_raises_when_window_empty():
    with pytest.raises(ValueError, match="no decision dates after"):
        report_and_filter(_DATES, model_cutoff="2030-01-01", clean_only=True)


def test_unset_cutoff_runs_all():
    out = report_and_filter(_DATES, model_cutoff=None, clean_only=False)
    assert out == sorted(_DATES)
