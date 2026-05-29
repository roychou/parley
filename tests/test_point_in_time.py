"""Tests for the point-in-time data layer helpers.

These tests exercise the as-of-date selection logic without hitting FMP.
End-to-end integration with real FMP is validated by the manual smoke run
in the Day 41 session log.
"""
import json

import pytest

from src.data import fundamentals as fund_mod
from src.data.fundamentals import (
    ValuationSnapshot,
    _build_raw_from_filings,
    get_fundamentals_as_of,
)


# ==========================================
# _build_raw_from_filings — pure builder
# ==========================================


def test_build_raw_from_filings_uses_filing_date_for_report_date():
    latest_income = {
        "date": "2025-06-30",            # period end
        "filingDate": "2025-07-30",       # actual filing date
        "acceptedDate": "2025-07-30 16:11:40",
        "revenue": 280_000_000_000,
        "netIncome": 100_000_000_000,
        "epsDiluted": 13.50,
    }
    prior_income = {"date": "2024-06-30", "revenue": 245_000_000_000}
    latest_balance = {
        "date": "2025-06-30",
        "totalDebt": 100_000_000_000,
        "totalStockholdersEquity": 340_000_000_000,
    }

    out = _build_raw_from_filings(latest_income, prior_income, latest_balance)

    assert out["report_date"] == "2025-07-30"          # filing date, not period end
    assert out["period_end_date"] == "2025-06-30"
    assert out["diluted_eps"] == pytest.approx(13.50)
    assert out["profit_margin"] == pytest.approx(100 / 280)
    # YoY: (280 - 245) / 245
    assert out["rev_growth_yoy"] == pytest.approx((280 - 245) / 245)
    assert out["debt_to_equity"] == pytest.approx(100 / 340)


def test_build_raw_from_filings_falls_back_to_accepted_date():
    # No filingDate — use acceptedDate
    latest_income = {
        "date": "2025-06-30",
        "acceptedDate": "2025-07-30 16:11:40",
        "revenue": 100, "netIncome": 20, "epsDiluted": 1.0,
    }
    prior_income = {"date": "2024-06-30", "revenue": 90}
    latest_balance = {"date": "2025-06-30", "totalDebt": 50, "totalStockholdersEquity": 100}

    out = _build_raw_from_filings(latest_income, prior_income, latest_balance)
    assert out["report_date"] == "2025-07-30"   # space-split takes the date part


# ==========================================
# get_fundamentals_as_of — point-in-time selection
# ==========================================


def test_get_fundamentals_as_of_picks_most_recent_eligible_filing(monkeypatch, tmp_path):
    """Asserts the as-of selection picks the latest filing with report_date <= as_of_date."""
    # Stub the filings cache to return three known filings
    fake_filings = [
        {"report_date": "2025-07-30", "period_end_date": "2025-06-30", "diluted_eps": 13.0,
         "profit_margin": 0.36, "rev_growth_yoy": 0.15, "debt_to_equity": 0.33},
        {"report_date": "2024-07-30", "period_end_date": "2024-06-30", "diluted_eps": 11.5,
         "profit_margin": 0.35, "rev_growth_yoy": 0.16, "debt_to_equity": 0.40},
        {"report_date": "2023-07-27", "period_end_date": "2023-06-30", "diluted_eps": 9.5,
         "profit_margin": 0.34, "rev_growth_yoy": 0.07, "debt_to_equity": 0.50},
    ]
    monkeypatch.setattr(fund_mod, "get_filings_history", lambda ticker: fake_filings)
    # Stub the price loader to return a fixed price on the as-of date
    monkeypatch.setattr(
        fund_mod,
        "_get_prices_dict",
        lambda ticker, period="5y": {"2024-09-15": {"close": 420.0}, "2024-09-16": {"close": 425.0}},
    )

    # As of 2024-09-15: latest eligible filing is 2024-07-30 (NOT 2025-07-30 — that hadn't happened yet)
    snap = get_fundamentals_as_of("TEST", "2024-09-15")
    assert snap is not None
    assert snap.report_date == "2024-07-30"
    assert snap.period_end_date == "2024-06-30"
    assert snap.diluted_eps == pytest.approx(11.5)
    # P/E uses the price on as-of date: 420 / 11.5
    assert snap.pe_ratio == pytest.approx(420.0 / 11.5)
    assert snap.price_date == "2024-09-15"


def test_get_fundamentals_as_of_returns_none_when_no_eligible_filing(monkeypatch):
    """If as_of_date predates every filing, return None."""
    fake_filings = [
        {"report_date": "2025-07-30", "period_end_date": "2025-06-30", "diluted_eps": 13.0,
         "profit_margin": 0.36, "rev_growth_yoy": 0.15, "debt_to_equity": 0.33},
    ]
    monkeypatch.setattr(fund_mod, "get_filings_history", lambda ticker: fake_filings)
    monkeypatch.setattr(
        fund_mod, "_get_prices_dict", lambda ticker, period="5y": {"2020-01-01": {"close": 100.0}}
    )

    snap = get_fundamentals_as_of("TEST", "2020-01-01")
    assert snap is None


def test_get_fundamentals_as_of_falls_back_to_most_recent_price_before_date(monkeypatch):
    """If as_of_date is a non-trading day (e.g., weekend), use the most recent close <= date."""
    fake_filings = [
        {"report_date": "2025-07-30", "period_end_date": "2025-06-30", "diluted_eps": 13.0,
         "profit_margin": 0.36, "rev_growth_yoy": 0.15, "debt_to_equity": 0.33},
    ]
    monkeypatch.setattr(fund_mod, "get_filings_history", lambda ticker: fake_filings)
    # Saturday "2026-03-14" has no price; Friday 2026-03-13 does
    monkeypatch.setattr(
        fund_mod,
        "_get_prices_dict",
        lambda ticker, period="5y": {"2026-03-13": {"close": 395.0}},
    )

    snap = get_fundamentals_as_of("TEST", "2026-03-14")
    assert snap is not None
    assert snap.price_date == "2026-03-13"  # fallback to Friday close
    assert snap.pe_ratio == pytest.approx(395.0 / 13.0)
