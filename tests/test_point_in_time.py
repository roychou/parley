"""Tests for the point-in-time data layer helpers.

These tests exercise the as-of-date selection logic with stubbed filings (no
network). End-to-end EDGAR extraction is covered by tests/test_edgar.py.
"""

import pytest

from src.data import fundamentals as fund_mod
from src.data.fundamentals import (
    get_fundamentals_as_of,
)

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
        lambda ticker, period="5y": {
            "2024-09-15": {"close": 420.0}, "2024-09-16": {"close": 425.0}
        },
    )

    # As of 2024-09-15: latest eligible filing is 2024-07-30
    # (NOT 2025-07-30 — that hadn't happened yet)
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
    monkeypatch.setattr(fund_mod, "_fmp_fundamentals_as_of", lambda *a, **k: None)  # no fallback

    snap = get_fundamentals_as_of("TEST", "2020-01-01")
    assert snap is None


def test_get_fundamentals_as_of_falls_back_to_most_recent_price_before_date(monkeypatch):
    """If as_of_date is a non-trading day (e.g., weekend), use the most recent close <= date."""
    # Filing kept recent relative to as_of so the recency guard doesn't (correctly) skip it.
    fake_filings = [
        {"report_date": "2026-01-30", "period_end_date": "2025-12-31", "diluted_eps": 13.0,
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


def test_get_fundamentals_as_of_skips_grossly_stale_filing(monkeypatch):
    """Recency guard: when the newest available filing is grossly stale relative to
    as_of (e.g. a concept-migration or parsing gap froze the history years back), the
    layer abstains (None -> the replay/specialist skips the name) rather than trading
    on years-old fundamentals. A fresh filing at the same as_of resolves normally."""
    prices = {"2026-05-15": {"close": 100.0}}
    monkeypatch.setattr(
        fund_mod, "_get_prices_dict", lambda ticker, period="5y": prices
    )
    monkeypatch.setattr(fund_mod, "_fmp_fundamentals_as_of", lambda *a, **k: None)  # no fallback

    stale = [
        {"report_date": "2020-02-20", "period_end_date": "2019-12-31", "diluted_eps": 5.0,
         "profit_margin": 0.30, "rev_growth_yoy": 0.10, "debt_to_equity": 0.40},
    ]
    monkeypatch.setattr(fund_mod, "get_filings_history", lambda ticker: stale)
    assert get_fundamentals_as_of("STALE", "2026-05-15") is None  # ~6yr stale → skip

    fresh = [
        {"report_date": "2026-05-01", "period_end_date": "2026-03-28", "diluted_eps": 5.0,
         "profit_margin": 0.30, "rev_growth_yoy": 0.10, "debt_to_equity": 0.40},
    ]
    monkeypatch.setattr(fund_mod, "get_filings_history", lambda ticker: fresh)
    assert get_fundamentals_as_of("FRESH", "2026-05-15") is not None  # 14d old → ok


# ==========================================
# FMP fallback (foreign 20-F/40-F filers EDGAR can't serve)
# ==========================================


def _stub_fmp(monkeypatch):
    """Stub FMP's stable income-statement + ratios (EUR reporter, two annual quarters)."""
    import src.data.fmp_client as fmp
    inc = [
        {"date": "2026-03-31", "filingDate": "2026-04-15", "fiscalYear": "2026", "period": "Q1",
         "revenue": 120.0, "epsdiluted": 1.2, "reportedCurrency": "EUR"},
        {"date": "2025-03-31", "filingDate": "2025-04-15", "fiscalYear": "2025", "period": "Q1",
         "revenue": 100.0, "epsdiluted": 1.0, "reportedCurrency": "EUR"},
    ]
    ratios = [
        {"date": "2026-03-31", "fiscalYear": "2026", "period": "Q1",
         "netProfitMargin": 0.25, "debtToEquityRatio": 0.5, "priceToEarningsRatio": 30.0},
    ]
    monkeypatch.setattr(fmp, "_get", lambda path, params=None: inc if "income" in path else ratios)


def test_fmp_fallback_builds_currency_safe_snapshot(monkeypatch):
    """FMP fallback: unit-free ratios (margin, YoY growth, D/E, P/E) come straight from
    FMP regardless of reporting currency; point-in-time gated on filingDate."""
    _stub_fmp(monkeypatch)
    monkeypatch.setattr(fund_mod, "_get_prices_dict",
                        lambda t, period="5y": {"2026-05-01": {"close": 90.0}})
    snap = fund_mod._fmp_fundamentals_as_of("ASML", "2026-05-01", "5y")
    assert snap is not None
    assert snap.report_date == "2026-04-15" and snap.period_end_date == "2026-03-31"
    assert snap.rev_growth_yoy == pytest.approx(0.20)   # (120-100)/100
    assert snap.profit_margin == pytest.approx(0.25)
    assert snap.pe_ratio == pytest.approx(30.0)
    assert snap.debt_to_equity == pytest.approx(0.5)


def test_fmp_fallback_respects_filing_date(monkeypatch):
    """Before the Q1 filingDate, the latest period isn't available; the only earlier
    period is a year stale, so the recency guard returns None (no look-ahead)."""
    _stub_fmp(monkeypatch)
    monkeypatch.setattr(fund_mod, "_get_prices_dict",
                        lambda t, period="5y": {"2026-04-10": {"close": 90.0}})
    assert fund_mod._fmp_fundamentals_as_of("ASML", "2026-04-10", "5y") is None


def test_edgar_stale_period_falls_back_to_fmp(monkeypatch):
    """A recently-filed filing covering a year-old period (foreign filer misparse, e.g.
    SHOP) fails the period-staleness guard, so get_fundamentals_as_of uses FMP."""
    # report_date recent, but period_end ~16 months stale -> EDGAR path returns None
    edgar_rows = [
        {"report_date": "2026-01-15", "period_end_date": "2024-09-30", "diluted_eps": 1.0,
         "profit_margin": 0.1, "rev_growth_yoy": 0.1, "debt_to_equity": 0.2},
    ]
    monkeypatch.setattr(fund_mod, "get_filings_history", lambda ticker: edgar_rows)
    monkeypatch.setattr(fund_mod, "_get_prices_dict",
                        lambda t, period="5y": {"2026-02-06": {"close": 50.0}})
    sentinel = object()
    monkeypatch.setattr(fund_mod, "_fmp_fundamentals_as_of", lambda *a, **k: sentinel)
    assert get_fundamentals_as_of("SHOP", "2026-02-06") is sentinel
