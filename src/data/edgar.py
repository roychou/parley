"""
SEC EDGAR data source: point-in-time fundamentals from XBRL company facts.

Replaces FMP as the fundamentals source (FMP free-tier capped statement history
at 5 and gated quarterly/constituents). EDGAR is free, has deep history, and is
natively point-in-time — every fact carries a real `filed` date. See
notes/edgar-design.md.

`build_filings_history(ticker)` returns the same shape the rest of the system
already consumes (the list of per-quarter raw dicts that `get_filings_history`
produced from FMP), so `get_fundamentals_as_of` is unchanged — only the source
underneath swaps.

XBRL shape notes (confirmed against MSFT):
- Flow metrics (revenue, net income, EPS) are *duration* facts with start+end.
  companyfacts mixes true quarters (~90-day span) with YTD cumulations (183/274-
  day) and the annual FY (~365). We keep the ~90-day quarters and derive Q4 from
  the annual minus the three reported quarters.
- Stock metrics (equity, debt) are *instant* facts (end only, no start); we take
  the value at each quarter's period-end date.
- Point-in-time: each quarter's `report_date` is the earliest `filed` among its
  facts (when it was first published). Restatements filed later are not applied
  (we use values as originally filed — the correct PIT semantic for v1).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import requests

from src.data.fundamentals import calc_debt_equity, calc_growth_yoy, calc_margin

logger = logging.getLogger(__name__)

SEC_DATA_BASE = "https://data.sec.gov"
SEC_WWW_BASE = "https://www.sec.gov"
TIMEOUT_SECONDS = 30

CACHE_DIR = Path("data/cache/edgar")
REF_DIR = Path("data/reference")

# Revenue is tagged differently across eras/filers — try in priority order.
REVENUE_CONCEPTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
# Total debt: prefer the combined tag; fall back to current + noncurrent components.
DEBT_TOTAL_CONCEPTS = ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount"]
DEBT_NONCURRENT_CONCEPTS = ["LongTermDebtNoncurrent"]
DEBT_CURRENT_CONCEPTS = ["LongTermDebtCurrent", "DebtCurrent"]

_QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 80, 100
_ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS = 350, 380


class EdgarError(RuntimeError):
    """Raised when EDGAR returns an error or unexpected shape."""


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        raise EdgarError(
            "EDGAR_USER_AGENT is not set. SEC requires a User-Agent with contact info "
            "(e.g. 'parley-research you@example.com'). Add it to .env."
        )
    return ua


def _get(url: str) -> Any:
    try:
        resp = requests.get(url, headers={"User-Agent": _user_agent()}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise EdgarError(f"EDGAR request failed: {e}") from e
    if resp.status_code != 200:
        raise EdgarError(f"EDGAR returned {resp.status_code} for {url}: {resp.text[:200]}")
    return resp.json()


# ==========================================
# TICKER -> CIK (SEC's authoritative map)
# ==========================================


def _cik_map_path() -> Path:
    return REF_DIR / "company_tickers.json"


def _load_cik_map() -> dict[str, str]:
    """Returns {TICKER: zero-padded 10-digit CIK}. Cached on disk under data/reference."""
    path = _cik_map_path()
    if not path.exists():
        REF_DIR.mkdir(parents=True, exist_ok=True)
        data = _get(f"{SEC_WWW_BASE}/files/company_tickers.json")
        path.write_text(json.dumps(data))
    else:
        data = json.loads(path.read_text())
    out: dict[str, str] = {}
    for row in data.values():
        out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return out


def ticker_to_cik(ticker: str) -> str:
    cik = _load_cik_map().get(ticker.upper())
    if cik is None:
        raise EdgarError(f"No CIK found for ticker {ticker} in SEC company_tickers map")
    return cik


# ==========================================
# COMPANY FACTS (cached)
# ==========================================


def _submissions(ticker: str) -> dict:
    """Cached SEC submissions blob for a ticker (lean — far cheaper than companyfacts)."""
    cik = ticker_to_cik(ticker)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    cache_path = CACHE_DIR / f"{ticker.upper()}_submissions_{today}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = _get(f"{SEC_DATA_BASE}/submissions/CIK{cik}.json")
    cache_path.write_text(json.dumps(data))
    return data


def recent_filings(ticker: str, forms: tuple[str, ...] = ("10-Q", "10-K")) -> list[dict]:
    """Structured recent filings, newest first: {form, filed, accession, primary_document}.

    `accession` is dash-stripped (ready for the Archives URL). Used by the sentiment
    specialist to locate the actual filing document.
    """
    recent = _submissions(ticker).get("filings", {}).get("recent", {})
    forms_list = recent.get("form", [])
    filed_list = recent.get("filingDate", [])
    acc_list = recent.get("accessionNumber", [])
    doc_list = recent.get("primaryDocument", [])
    out = [
        {
            "form": form,
            "filed": filed,
            "accession": acc.replace("-", ""),
            "primary_document": doc,
        }
        for form, filed, acc, doc in zip(forms_list, filed_list, acc_list, doc_list)
        if form in forms
    ]
    out.sort(key=lambda f: f["filed"], reverse=True)
    return out


def recent_filing_dates(ticker: str, forms: tuple[str, ...] = ("10-Q", "10-K")) -> list[str]:
    """Sorted filing dates (YYYY-MM-DD) for the given forms — the event screen's "did
    this name just file?" check. Derived from the same cached submissions blob."""
    return sorted(f["filed"] for f in recent_filings(ticker, forms))


def _get_text(url: str) -> str:
    """GET a URL and return raw text (filing documents are HTML, not JSON)."""
    try:
        resp = requests.get(url, headers={"User-Agent": _user_agent()}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise EdgarError(f"EDGAR request failed: {e}") from e
    if resp.status_code != 200:
        raise EdgarError(f"EDGAR returned {resp.status_code} for {url}: {resp.text[:200]}")
    return resp.text


def fetch_company_facts(ticker: str) -> dict:
    """Fetch (and cache) the full XBRL company-facts blob for a ticker."""
    cik = ticker_to_cik(ticker)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    cache_path = CACHE_DIR / f"{ticker.upper()}_{today}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = _get(f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json")
    cache_path.write_text(json.dumps(data))
    return data


# ==========================================
# EXTRACTION HELPERS
# ==========================================


def _span_days(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    y1, m1, d1 = map(int, start.split("-"))
    y2, m2, d2 = map(int, end.split("-"))
    return (date(y2, m2, d2) - date(y1, m1, d1)).days


def _concept_rows(gaap: dict, names: list[str], unit: str = "USD") -> list[dict]:
    """Rows for the first present concept in `names` (priority fallback)."""
    for name in names:
        node = gaap.get(name)
        if node and unit in node.get("units", {}):
            return node["units"][unit]
    return []


def _duration_by_end(rows: list[dict], min_days: int, max_days: int) -> dict[str, dict]:
    """Map period-end date -> earliest-filed duration record within [min,max]-day span.

    Keyed on the fact's own `end` date, NOT fy/fp (those reflect the filing's
    fiscal context, not the fact's period — a known XBRL trap). Earliest `filed`
    gives the value as originally reported (point-in-time).
    """
    out: dict[str, dict] = {}
    for r in rows:
        end = r.get("end")
        if not end:
            continue
        days = _span_days(r.get("start"), end)
        if days is None or not (min_days <= days <= max_days):
            continue
        if end not in out or r.get("filed", "") < out[end].get("filed", ""):
            out[end] = r
    return out


def _quarter_flow(rows: list[dict]) -> dict[str, dict]:
    """End-date -> true-quarter (~90-day) record for a duration (flow) concept."""
    return _duration_by_end(rows, _QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS)


def _annual_flow(rows: list[dict]) -> dict[str, dict]:
    """Fiscal-year-end -> annual (~365-day) record for a duration concept."""
    return _duration_by_end(rows, _ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS)


def _end_minus_one_year(end: str) -> str:
    """Same month/day, prior year (fiscal quarter-ends are consistent MM-DD)."""
    y, m, d = end.split("-")
    return f"{int(y) - 1:04d}-{m}-{d}"


def _instant_at(rows: list[dict], end_date: str) -> float | None:
    """Value of an instant (stock) concept at a given period-end date, earliest filed."""
    best: dict | None = None
    for r in rows:
        if r.get("start") is not None or r.get("end") != end_date:
            continue
        if best is None or r.get("filed", "") < best.get("filed", ""):
            best = r
    return float(best["val"]) if best else None


def _total_debt_at(gaap: dict, end_date: str) -> float:
    """Total debt at a period-end: prefer a combined tag, else current + noncurrent."""
    for name in DEBT_TOTAL_CONCEPTS:
        v = _instant_at(_concept_rows(gaap, [name]), end_date)
        if v is not None:
            return v
    nonc = _instant_at(_concept_rows(gaap, DEBT_NONCURRENT_CONCEPTS), end_date)
    curr = _instant_at(_concept_rows(gaap, DEBT_CURRENT_CONCEPTS), end_date)
    if nonc is None and curr is None:
        return float("nan")
    return (nonc or 0.0) + (curr or 0.0)


# ==========================================
# BUILD FILINGS HISTORY (existing consumer shape)
# ==========================================


def build_filings_history(ticker: str) -> list[dict]:
    """Per-quarter point-in-time fundamentals for `ticker`, most recent first.

    Same dict shape `get_fundamentals_as_of` already consumes:
    {report_date, period_end_date, diluted_eps, profit_margin, rev_growth_yoy,
     debt_to_equity}. Q4 (flow metrics) is derived as FY minus the three reported
     quarters. rev_growth_yoy compares the same fiscal period one year prior.
    """
    facts = fetch_company_facts(ticker)
    gaap = facts.get("facts", {}).get("us-gaap", {})
    if not gaap:
        raise EdgarError(f"No us-gaap facts for {ticker}")

    rev_rows = _concept_rows(gaap, REVENUE_CONCEPTS)
    ni_rows = _concept_rows(gaap, ["NetIncomeLoss"])
    eps_rows = _concept_rows(gaap, ["EarningsPerShareDiluted"], unit="USD/shares")
    eq_rows = _concept_rows(gaap, ["StockholdersEquity"])

    rev_q = _quarter_flow(rev_rows)   # all keyed by period-end date
    ni_q = _quarter_flow(ni_rows)
    eps_q = _quarter_flow(eps_rows)
    rev_fy = _annual_flow(rev_rows)
    ni_fy = _annual_flow(ni_rows)
    eps_fy = _annual_flow(eps_rows)

    # Derive Q4 (flow) = FY - (the three quarters ending within that fiscal year).
    def _derive_q4(fy_map: dict[str, dict], q_map: dict[str, dict]) -> None:
        for fye, fy_rec in fy_map.items():
            if fye in q_map:
                continue
            prior_fye = _end_minus_one_year(fye)
            parts = [q for end, q in q_map.items() if prior_fye < end < fye]
            if len(parts) != 3:
                continue
            q_map[fye] = {
                "end": fye,
                "val": fy_rec["val"] - sum(p["val"] for p in parts),
                "filed": fy_rec.get("filed", ""),
            }

    _derive_q4(rev_fy, rev_q)
    _derive_q4(ni_fy, ni_q)
    _derive_q4(eps_fy, eps_q)

    # Trailing-twelve-month diluted EPS: P/E must use TTM, not a single quarter
    # (a quarterly EPS would inflate P/E ~4x). TTM = the four quarters ending at E.
    eps_ends = sorted(eps_q)

    def _ttm_eps(end: str) -> float:
        if end not in eps_q:
            return float("nan")
        i = eps_ends.index(end)
        if i < 3:
            return float("nan")
        window = eps_ends[i - 3:i + 1]
        return sum(float(eps_q[e]["val"]) for e in window)

    filings: list[dict] = []
    for end, rev_rec in rev_q.items():
        revenue = float(rev_rec["val"])
        ni_rec = ni_q.get(end)
        prior_rev = rev_q.get(_end_minus_one_year(end))

        net_income = float(ni_rec["val"]) if ni_rec else float("nan")
        equity = _instant_at(eq_rows, end)
        equity = equity if equity is not None else float("nan")
        total_debt = _total_debt_at(gaap, end)

        filings.append({
            "report_date": rev_rec.get("filed", ""),
            "period_end_date": end,
            "diluted_eps": _ttm_eps(end),  # TTM, for a meaningful P/E
            "profit_margin": calc_margin(net_income, revenue),
            "rev_growth_yoy": (
                calc_growth_yoy(revenue, float(prior_rev["val"])) if prior_rev else float("nan")
            ),
            "debt_to_equity": calc_debt_equity(total_debt, equity),
        })

    filings.sort(key=lambda f: f["report_date"], reverse=True)
    return filings
