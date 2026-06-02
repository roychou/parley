"""Offline tests for the EDGAR XBRL extraction logic.

A synthetic companyfacts fixture (calendar fiscal year, Dec 31 year-end)
exercises the tricky bits: ~90-day quarter filtering vs YTD traps, Q4 derivation
from the annual, same-quarter-prior-year YoY, TTM EPS for P/E, and debt fallback.
"""
import src.data.edgar as edgar


def _dur(start, end, val, filed):
    return {"start": start, "end": end, "val": val, "filed": filed}


def _inst(end, val, filed):
    return {"start": None, "end": end, "val": val, "filed": filed}


# Calendar-year filer. Revenue: true quarters (~91d) + annual FY (~365d) + one YTD trap.
_REVENUE = [
    _dur("2023-01-01", "2023-03-31", 100, "2023-04-25"),
    _dur("2023-04-01", "2023-06-30", 110, "2023-07-25"),
    _dur("2023-07-01", "2023-09-30", 120, "2023-10-25"),
    _dur("2023-01-01", "2023-12-31", 450, "2024-02-20"),  # FY2023 → Q4 = 120
    _dur("2024-01-01", "2024-03-31", 130, "2024-04-25"),
    _dur("2024-04-01", "2024-06-30", 140, "2024-07-25"),
    _dur("2024-01-01", "2024-09-30", 420, "2024-10-25"),  # YTD trap (273d) — must be ignored
    _dur("2024-07-01", "2024-09-30", 150, "2024-10-25"),
    _dur("2024-01-01", "2024-12-31", 600, "2025-02-20"),  # FY2024 → Q4 = 180
]
_NET_INCOME = [
    _dur("2024-07-01", "2024-09-30", 30, "2024-10-25"),  # margin 30/150 = 20%
]
_EPS = [
    _dur("2023-01-01", "2023-03-31", 1.0, "2023-04-25"),
    _dur("2023-04-01", "2023-06-30", 1.1, "2023-07-25"),
    _dur("2023-07-01", "2023-09-30", 1.2, "2023-10-25"),
    _dur("2023-01-01", "2023-12-31", 4.5, "2024-02-20"),  # FY → Q4 EPS = 1.2
    _dur("2024-01-01", "2024-03-31", 1.3, "2024-04-25"),
    _dur("2024-04-01", "2024-06-30", 1.4, "2024-07-25"),
    _dur("2024-07-01", "2024-09-30", 1.5, "2024-10-25"),
]
_EQUITY = [_inst("2024-09-30", 500, "2024-10-25")]
_DEBT = [_inst("2024-09-30", 100, "2024-10-25")]

_FACTS = {"facts": {"us-gaap": {
    "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": _REVENUE}},
    "NetIncomeLoss": {"units": {"USD": _NET_INCOME}},
    "EarningsPerShareDiluted": {"units": {"USD/shares": _EPS}},
    "StockholdersEquity": {"units": {"USD": _EQUITY}},
    "LongTermDebt": {"units": {"USD": _DEBT}},
}}}


def _build(monkeypatch):
    monkeypatch.setattr(edgar, "fetch_company_facts", lambda ticker: _FACTS)
    return {f["period_end_date"]: f for f in edgar.build_filings_history("TEST")}


def test_match_prior_year_tolerates_fiscal_drift():
    # 52/53-week fiscal calendars drift a day or two year over year; the prior-year
    # quarter must still match within tolerance (exact same-MM-DD would yield NaN YoY).
    assert edgar._match_prior_year("2025-03-28", ["2024-03-29", "2025-06-28"]) == "2024-03-29"
    # picks the nearest to ~365d prior when several candidates exist
    assert edgar._match_prior_year("2025-03-28", ["2024-03-29", "2024-06-30"]) == "2024-03-29"
    # nothing within ~1yr ± tolerance → None (don't fabricate a prior period)
    assert edgar._match_prior_year("2025-06-28", ["2025-03-28"]) is None


def test_best_revenue_rows_prefers_most_recent_coverage():
    """Two concepts both expose quarters, but one is frozen years back (a deprecated
    tag the filer migrated away from). Pick the concept reaching the most recent
    period — the NVDA/XOM staleness bug, where the first present concept stopped in
    2019/2023 while current revenue lives under another tag."""
    stale = [_dur("2019-01-01", "2019-03-31", 100, "2019-04-30"),
             _dur("2019-04-01", "2019-06-30", 110, "2019-07-30")]
    current = [_dur("2025-01-01", "2025-03-31", 500, "2025-04-30"),
               _dur("2025-04-01", "2025-06-30", 520, "2025-07-30")]
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": stale}},
        "Revenues": {"units": {"USD": current}},
    }
    rows = edgar._best_revenue_rows(gaap)
    assert any(r["val"] in (500, 520) for r in rows)        # picked the current concept
    assert all(r["val"] not in (100, 110) for r in rows)    # not the frozen one


def test_span_filtering_ignores_ytd_trap(monkeypatch):
    by_end = _build(monkeypatch)
    # The 273-day YTD entry for 2024-09-30 must not override the true 91-day quarter.
    # True Q3 2024 revenue is 150; YTD would be 420. Verify via YoY: 150 vs 120 = +25%.
    assert by_end["2024-09-30"]["rev_growth_yoy"] == 0.25


def test_q4_derived_from_annual(monkeypatch):
    by_end = _build(monkeypatch)
    # FY2023 = 450, Q1+Q2+Q3 = 330 → Q4 (end 2023-12-31) exists, YoY needs no prior.
    assert "2023-12-31" in by_end
    # Q4 2024 YoY: rev 180 (600-420... derived) vs 120 → +50%
    assert by_end["2024-12-31"]["rev_growth_yoy"] == 0.50


def test_report_date_is_original_filing(monkeypatch):
    by_end = _build(monkeypatch)
    assert by_end["2024-09-30"]["report_date"] == "2024-10-25"


def test_margin_uses_quarter(monkeypatch):
    by_end = _build(monkeypatch)
    assert by_end["2024-09-30"]["profit_margin"] == 30 / 150


def test_ttm_eps_sums_trailing_four_quarters(monkeypatch):
    by_end = _build(monkeypatch)
    # TTM at 2024-09-30 = Q4'23(1.2) + Q1'24(1.3) + Q2'24(1.4) + Q3'24(1.5) = 5.4
    assert abs(by_end["2024-09-30"]["diluted_eps"] - 5.4) < 1e-9
    # Earliest quarters lack a full trailing year → NaN
    assert by_end["2023-03-31"]["diluted_eps"] != by_end["2023-03-31"]["diluted_eps"]  # NaN


def test_debt_to_equity(monkeypatch):
    by_end = _build(monkeypatch)
    assert by_end["2024-09-30"]["debt_to_equity"] == 100 / 500


def test_total_debt_fallback_to_components(monkeypatch):
    # No combined LongTermDebt tag → assemble from noncurrent + current.
    gaap = {
        "LongTermDebtNoncurrent": {"units": {"USD": [_inst("2024-09-30", 80, "2024-10-25")]}},
        "LongTermDebtCurrent": {"units": {"USD": [_inst("2024-09-30", 20, "2024-10-25")]}},
    }
    assert edgar._total_debt_at(gaap, "2024-09-30") == 100


def test_best_revenue_rows_skips_annual_only_concept():
    """When the first revenue concept has only annual/YTD durations (no ~90-day
    quarters) but a later one carries quarters, pick the later one (BA/LW/TDG case)."""
    # ExcludingAssessedTax: only a 365-day period (no quarters). Revenues: 90-day quarters.
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 400, "filed": "2026-02-01"},
        ]}},
        "Revenues": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-03-31", "val": 100, "filed": "2025-04-30"},
            {"start": "2025-04-01", "end": "2025-06-30", "val": 110, "filed": "2025-07-30"},
        ]}},
    }
    rows = edgar._best_revenue_rows(gaap)
    # picked "Revenues" (the one with quarterly flow), not the annual-only first concept
    assert edgar._quarter_flow(rows)  # non-empty quarters
    assert any(r["val"] in (100, 110) for r in rows)


def test_build_annual_any_ifrs_eur_filer(monkeypatch):
    """Foreign IFRS filer (annual, EUR): margin & YoY from annual figures; EPS NaN
    (non-USD, so no currency-mismatched P/E); freq 'annual'."""
    facts = {"facts": {"ifrs-full": {
        "Revenue": {"units": {"EUR": [
            _dur("2024-01-01", "2024-12-31", 100, "2025-02-20"),
            _dur("2025-01-01", "2025-12-31", 120, "2026-02-20"),
        ]}},
        "ProfitLoss": {"units": {"EUR": [_dur("2025-01-01", "2025-12-31", 30, "2026-02-20")]}},
        "Equity": {"units": {"EUR": [_inst("2025-12-31", 500, "2026-02-20")]}},
        "DilutedEarningsLossPerShare": {"units": {"EUR/shares": [
            _dur("2025-01-01", "2025-12-31", 2.5, "2026-02-20")]}},
    }}}
    monkeypatch.setattr(edgar, "fetch_company_facts", lambda t: facts)
    by = {x["period_end_date"]: x for x in edgar.build_filings_history("IFRSCO")}
    assert by["2025-12-31"]["freq"] == "annual"
    assert by["2025-12-31"]["rev_growth_yoy"] == 0.20      # (120-100)/100
    assert by["2025-12-31"]["profit_margin"] == 30 / 120
    eps = by["2025-12-31"]["diluted_eps"]
    assert eps != eps                                      # NaN — EUR reporter


def test_build_annual_any_usd_filer_keeps_eps(monkeypatch):
    """Annual filer reporting in USD (no quarterly data): EPS retained so P/E is valid."""
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            _dur("2024-01-01", "2024-12-31", 100, "2025-02-20"),
            _dur("2025-01-01", "2025-12-31", 110, "2026-02-20"),
        ]}},
        "NetIncomeLoss": {"units": {"USD": [_dur("2025-01-01", "2025-12-31", 22, "2026-02-20")]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            _dur("2025-01-01", "2025-12-31", 3.0, "2026-02-20")]}},
        "StockholdersEquity": {"units": {"USD": [_inst("2025-12-31", 200, "2026-02-20")]}},
    }}}
    monkeypatch.setattr(edgar, "fetch_company_facts", lambda t: facts)
    by = {x["period_end_date"]: x for x in edgar.build_filings_history("USDCO")}
    assert by["2025-12-31"]["freq"] == "annual"
    assert by["2025-12-31"]["diluted_eps"] == 3.0          # USD -> retained
    assert abs(by["2025-12-31"]["rev_growth_yoy"] - 0.10) < 1e-9
