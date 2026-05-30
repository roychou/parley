"""
Grab deep (≈30y) FMP fundamentals + ratios to local disk — for use during a
one-month FMP Premium subscription (then cancel; the data stays on disk).

The runtime fundamentals path uses SEC EDGAR (point-in-time, free, + filing text
for the sentiment specialist). This grab is *optionality*: FMP Premium reaches
back ~30y (vs EDGAR's ~2009 XBRL floor), so it covers the pre-2009 window and
serves as a cross-validation source. It stores raw per-ticker JSON and does NOT
wire into the runtime — decide later how/whether to use the pre-2009 reach.

Run AFTER the survivorship probe passes (paid tier), with FMP_API_KEY set:

    uv run python scripts/fmp_grab_fundamentals.py --start 1996-01-01

Resumable: skips tickers already grabbed today. Caveat: deep historical
fundamentals are lower point-in-time fidelity than EDGAR (vendor restatement
risk), especially pre-2009 — use with that awareness.
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path
load_dotenv()

from src.data.fmp_client import FMPError, _get  # noqa: E402
from src.data.universe import sp500_members_in_range  # noqa: E402

OUT_DIR = Path("data/cache/fmp_fundamentals")
# FMP stable endpoint -> key prefix in the stored blob.
ENDPOINTS = {
    "income-statement": "income",
    "balance-sheet-statement": "balance",
    "ratios": "ratios",
}


def grab_ticker(ticker: str, limit: int) -> str:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{ticker}.json"
    if path.exists():
        return "cached"
    blob: dict = {}
    for endpoint, prefix in ENDPOINTS.items():
        for period in ("quarter", "annual"):
            data = _get(endpoint, params={"symbol": ticker, "period": period, "limit": limit})
            blob[f"{prefix}_{period}"] = data
    path.write_text(json.dumps(blob))
    return "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description="Grab deep FMP fundamentals to disk.")
    parser.add_argument("--start", default="1996-01-01", help="Universe window start.")
    parser.add_argument("--end", default=None, help="Universe window end (default today).")
    parser.add_argument("--limit", type=int, default=120, help="Statements/call (~30y quarterly).")
    parser.add_argument("--sleep", type=float, default=0.1, help="Inter-ticker throttle (s).")
    args = parser.parse_args()

    end = args.end or date.today().isoformat()
    tickers = sp500_members_in_range(args.start, end)
    print(f"Grabbing FMP fundamentals for {len(tickers)} tickers (limit={args.limit})...")

    ok = cached = 0
    errors: list[tuple[str, str]] = []
    for i, ticker in enumerate(tickers, 1):
        try:
            result = grab_ticker(ticker, args.limit)
            ok += result == "ok"
            cached += result == "cached"
            if result == "ok":
                time.sleep(args.sleep)
        except FMPError as e:
            errors.append((ticker, str(e)[:80]))
        if i % 100 == 0:
            print(f"  {i}/{len(tickers)} (ok={ok}, cached={cached}, errors={len(errors)})")

    print("\n" + "=" * 56)
    print(f"FMP fundamentals grab: {ok} fetched, {cached} cached, {len(errors)} errors")
    for ticker, msg in errors[:15]:
        print(f"  - {ticker}: {msg}")
    print("=" * 56)


if __name__ == "__main__":
    main()
