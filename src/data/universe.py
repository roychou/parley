"""
Point-in-time Nasdaq-100 membership — the bot's eligible universe.

Index membership is reference data, separate from our market-data/execution vendor
(IBKR exposes no constituents endpoint). The Invesco QQQ Trust (CIK 1067839) tracks
the Nasdaq-100 and discloses its full holdings to the SEC quarterly on Form N-PORT,
so we derive authoritative point-in-time membership from those filings — the same
source (EDGAR) as our fundamentals. Quarterly granularity, ~60-day filing lag.

N-PORT identifies each holding by name + CUSIP (no ticker). We resolve tickers via
SEC's company_tickers.json (normalized name match) plus a CUSIP override table for
dual-class / OTC-collision names (e.g. Alphabet GOOGL vs GOOG). Anything unresolved
is logged and dropped — a ticker is never guessed into the tradable universe.

`nasdaq100_as_of(date)` is the eligible universe on a date (point-in-time, so
backtests are not survivorship-biased). `current_nasdaq100()` is for live use.

Membership is cached to data/reference/nasdaq100_membership.json. A new quarter's
N-PORT appears on EDGAR ~60 days after quarter-end; the cache is TTL-gated and
refreshes incrementally (only newly-filed periods are fetched).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

from src.data.edgar import EdgarError, _ensure_cik_map_fresh, _get, _get_text

logger = logging.getLogger(__name__)

QQQ_CIK = "1067839"  # Invesco QQQ Trust, Series 1 — tracks the Nasdaq-100
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{QQQ_CIK.zfill(10)}.json"
ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"

REF_DIR = Path("data/reference")
COMPANY_TICKERS = REF_DIR / "company_tickers.json"
CACHE_PATH = REF_DIR / "nasdaq100_membership.json"
_CACHE_MAX_AGE = dt.timedelta(days=7)  # re-check EDGAR for a newer N-PORT weekly

# CUSIP -> ticker overrides for holdings whose issuer name maps ambiguously to more
# than one ticker in company_tickers.json (dual share classes, OTC twins). CUSIP is
# the stable key. Verified against the QQQ N-PORT for period 2026-03-31.
CUSIP_TICKER: dict[str, str] = {
    "02079K305": "GOOGL",  # Alphabet Class A
    "02079K107": "GOOG",   # Alphabet Class C
    "20030N101": "CMCSA",  # Comcast (vs CCZ)
    "98389B100": "XEL",    # Xcel Energy (vs XELLL)
    "872590104": "TMUS",   # T-Mobile US (vs TMUSZ/I/L)
    "884903808": "TRI",    # Thomson Reuters (vs TMSOF, OTC)
    "595017104": "MCHP",   # Microchip Technology (vs MCHPP)
    "594972408": "MSTR",   # Strategy / MicroStrategy (vs STRC/F/K/D)
    "N07059210": "ASML",   # ASML Holding (vs ASMLF, OTC)
    "92532F100": "VRTX",   # Vertex Pharmaceuticals (vs VERX = Vertex Inc.)
    # Former constituents absent from SEC's *current* company_tickers.json
    # (acquired / taken private / renamed), needed for point-in-time history of
    # older N-PORT periods. CUSIP is permanent; many no longer trade (a backtest
    # simply skips names without price data).
    "30303M102": "META",   # Facebook -> Meta (same CUSIP)
    "35137L105": "FOXA",   # Fox Corp Class A
    "35137L204": "FOX",    # Fox Corp Class B
    "482480100": "KLAC",   # KLA-Tencor -> KLA
    "445658107": "JBHT",   # J.B. Hunt Transport Services
    "89677Q107": "TCOM",   # Trip.com Group
    "22943F100": "CTRP",   # Ctrip.com International (later Trip.com)
    "G5480U104": "LBTYA",  # Liberty Global Class A
    "G5480U120": "LBTYK",  # Liberty Global Class C
    "98980L101": "ZM",     # Zoom Video Communications
    "47215P106": "JD",     # JD.com
    "722304102": "PDD",    # Pinduoduo (later PDD Holdings)
    "056752108": "BIDU",   # Baidu
    "64110W102": "NTES",   # NetEase
    "931427108": "WBA",    # Walgreens Boots Alliance (taken private 2025)
    "03662Q105": "ANSS",   # ANSYS (acquired by Synopsys 2025)
    "848637104": "SPLK",   # Splunk (acquired by Cisco)
    "00507V109": "ATVI",   # Activision Blizzard (acquired by Microsoft)
    "81181C104": "SGEN",   # Seagen (acquired by Pfizer)
    "812578102": "SGEN",   # Seattle Genetics (pre-rename, same ticker)
    "156782104": "CERN",   # Cerner (acquired by Oracle)
    "015351109": "ALXN",   # Alexion (acquired by AstraZeneca)
    "57772K101": "MXIM",   # Maxim Integrated (acquired by ADI)
    "177376100": "CTXS",   # Citrix (taken private)
    "151020104": "CELG",   # Celgene (acquired by BMS)
    "871503108": "SYMC",   # Symantec (renamed NortonLifeLock/Gen Digital)
    "N59465109": "MYL",    # Mylan (merged into Viatris)
    "983919101": "XLNX",   # Xilinx (acquired by AMD)
}

_NAME_SUFFIXES = (
    r"\b(inc|incorporated|corp|corporation|co|company|companies|ltd|limited|plc|"
    r"nv|n v|sa|se|ag|the|holdings?|group|class|cl|common|stock|adr|ads|de|md|"
    r"pharmaceuticals?)\b"
)


# ==========================================
# TICKER RESOLUTION (name + CUSIP -> ticker)
# ==========================================


def _norm_name(s: str) -> str:
    """Normalize an issuer name for matching: drop parentheticals, the /DE state
    suffix, punctuation, and common entity-type words."""
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)   # drop parentheticals, e.g. "(The)"
    s = s.split("/")[0]              # drop "/DE"-style state suffix
    s = re.sub(r"[.,&'/\-]", " ", s)
    s = re.sub(_NAME_SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _name_index() -> dict[str, list[str]]:
    """normalized issuer name -> [tickers], from SEC's company_tickers.json."""
    _ensure_cik_map_fresh()
    raw = json.loads(COMPANY_TICKERS.read_text())
    rows = raw.values() if isinstance(raw, dict) else raw
    idx: dict[str, list[str]] = {}
    for r in rows:
        idx.setdefault(_norm_name(r["title"]), []).append(r["ticker"].upper())
    return idx


def _resolve_ticker(name: str, cusip: str, name_idx: dict[str, list[str]]) -> str | None:
    """CUSIP override first, then a unique normalized-name match. Returns None for an
    unknown or ambiguous name — the caller logs and drops it (never guess)."""
    if cusip in CUSIP_TICKER:
        return CUSIP_TICKER[cusip]
    cand = name_idx.get(_norm_name(name), [])
    return cand[0] if len(cand) == 1 else None


# ==========================================
# N-PORT PARSING + MEMBERSHIP BUILD
# ==========================================


def _nport_index() -> list[tuple[str, str]]:
    """[(period_end_date, accession_no_dashes)] for QQQ NPORT-P filings, newest first."""
    sub = _get(SUBMISSIONS_URL)
    rec = sub["filings"]["recent"]
    out = [
        (period, acc.replace("-", ""))
        for form, period, acc in zip(rec["form"], rec["reportDate"], rec["accessionNumber"])
        if form.startswith("NPORT-P") and period
    ]
    out.sort(reverse=True)
    return out


def _field(block: str, tag: str) -> str:
    m = re.findall(rf"<(?:\w+:)?{tag}[^>]*>(.*?)</(?:\w+:)?{tag}>", block, re.DOTALL)
    return m[0].strip() if m else ""


def _parse_filing(accession: str, name_idx: dict[str, list[str]]) -> list[str]:
    """Equity (assetCat=EC) holdings of one QQQ N-PORT, resolved to sorted tickers."""
    xml = _get_text(f"{ARCHIVE_BASE}/{QQQ_CIK}/{accession}/primary_doc.xml")
    tickers: set[str] = set()
    unresolved: list[str] = []
    for block in re.findall(r"<invstOrSec>.*?</invstOrSec>", xml, re.DOTALL):
        if _field(block, "assetCat") != "EC":  # equity common only (skip cash/MMF)
            continue
        name, cusip = _field(block, "name"), _field(block, "cusip")
        tk = _resolve_ticker(name, cusip, name_idx)
        if tk:
            tickers.add(tk)
        else:
            unresolved.append(f"{name} (cusip {cusip})")
    if unresolved:
        logger.warning(
            "Nasdaq-100 %s: %d holdings unresolved and dropped: %s",
            accession, len(unresolved), "; ".join(unresolved),
        )
    return sorted(tickers)


def build_membership(force: bool = False) -> list[dict]:
    """Point-in-time membership from QQQ N-PORT filings, newest first:
    [{period, accession, tickers}]. Cached + TTL-gated; refreshes incrementally
    (only periods not already cached are fetched). `force` rebuilds from scratch."""
    existing: list[dict] = []
    if CACHE_PATH.exists() and not force:
        existing = json.loads(CACHE_PATH.read_text())
        age = dt.datetime.now() - dt.datetime.fromtimestamp(CACHE_PATH.stat().st_mtime)
        if age < _CACHE_MAX_AGE:
            return existing

    have = {f["period"] for f in existing}
    name_idx = _name_index()
    fresh: list[dict] = []
    for period, acc in _nport_index():
        if period in have:
            continue
        try:
            tickers = _parse_filing(acc, name_idx)
        except EdgarError as e:
            logger.warning("Nasdaq-100: skipping N-PORT %s: %s", acc, e)
            continue
        if tickers:
            fresh.append({"period": period, "accession": acc, "tickers": tickers})

    merged = existing + fresh
    if not merged:
        raise EdgarError("Nasdaq-100: no membership parsed from QQQ N-PORT filings")
    merged.sort(key=lambda f: f["period"], reverse=True)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(merged, indent=2))
    if fresh:
        logger.info(
            "Nasdaq-100 membership: %d filings cached (%s..%s), %d new",
            len(merged), merged[-1]["period"], merged[0]["period"], len(fresh),
        )
    return merged


def _membership() -> list[dict]:
    return build_membership()


# ==========================================
# PUBLIC API (mirrors the former S&P 500 shape)
# ==========================================


def nasdaq100_as_of(date: str) -> list[str]:
    """Sorted Nasdaq-100 constituents as of `date` (YYYY-MM-DD): the most recent
    N-PORT period on/before `date`. Before the earliest filing, returns the earliest
    available set (documented approximation; the bot's clean window is recent)."""
    mem = _membership()
    eligible = [f for f in mem if f["period"] <= date]  # mem is newest-first
    chosen = eligible[0] if eligible else min(mem, key=lambda f: f["period"])
    return list(chosen["tickers"])


def nasdaq100_members_in_range(start_date: str, end_date: str) -> list[str]:
    """Sorted union of constituents across [start_date, end_date]: the filing in
    effect at start plus any filed within the window, so a name present anywhere in
    the window is included (for price/data backfill that must cover the whole span)."""
    mem = _membership()
    tickers: set[str] = set()
    in_effect = [f for f in mem if f["period"] <= start_date]
    if in_effect:
        tickers.update(in_effect[0]["tickers"])
    for f in mem:
        if start_date <= f["period"] <= end_date:
            tickers.update(f["tickers"])
    if not tickers and mem:
        tickers.update(min(mem, key=lambda f: f["period"])["tickers"])
    return sorted(tickers)


def current_nasdaq100() -> list[str]:
    """Latest available Nasdaq-100 constituents (most recent N-PORT). For live use."""
    return list(_membership()[0]["tickers"])


def membership_end(ticker: str) -> str | None:
    """The latest filing period in which `ticker` appeared IF it is not in the most
    recent filing, else None. Caps a departed name's price series against
    recycled-ticker contamination (mirrors the former S&P 500 guard)."""
    mem = _membership()
    if ticker in mem[0]["tickers"]:
        return None
    for f in mem:  # newest first
        if ticker in f["tickers"]:
            return f["period"]
    return None
