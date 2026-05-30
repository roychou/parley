"""
Bulk fundamentals grab (FMP Premium) — maximize the one-month subscription.

FMP's `*-bulk` endpoints return **all companies for one (year, period)** as CSV,
so deep fundamentals come in ~600 calls (≈30y × 4 quarters × ~5 statements) for
the whole market, vs ~7,200 per-ticker. Stored raw to disk; cancel after.

Prices are NOT here: deep price history is most efficient per-ticker
(`backfill --price-period max`, ~1,200 calls full-history each), since the only
price *bulk* endpoint is per-date (~7,560 calls for 30y). So:
    prices  -> uv run python -m src.backtest.backfill --start 1996-01-01 --price-period max
    bulk fundamentals -> this script.

Run AFTER confirming Premium (the script verifies a bulk endpoint before looping):

    uv run python scripts/fmp_grab_bulk.py --start-year 1996

Resumable (skips cached files). Endpoint paths/period values below are FMP's
documented bulk shapes — the verify step surfaces any that your tier/plan names
differently before a long run. The per-ticker `fmp_grab_fundamentals.py` is the
fallback if bulk endpoints aren't available on the tier.
"""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # repo root on path
load_dotenv(_ROOT / ".env")  # robust regardless of cwd

from src.data.fmp_client import FMPError, get_bulk_csv  # noqa: E402

OUT_DIR = Path("data/cache/fmp_bulk")
# Bulk statement endpoints (CSV, all companies per period). Verify against your plan.
STATEMENT_ENDPOINTS = [
    "income-statement-bulk",
    "balance-sheet-statement-bulk",
    "cash-flow-statement-bulk",
    "ratios-bulk",
    "key-metrics-bulk",
]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]


def _fetch(endpoint: str, params: dict, fname: str) -> str:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / fname
    if path.exists():
        return "cached"
    csv = get_bulk_csv(endpoint, params)
    path.write_text(csv)
    return "ok"


def _verify() -> None:
    """One bulk call to confirm the endpoint/tier before a long loop."""
    last_year = date.today().year - 1
    try:
        get_bulk_csv("income-statement-bulk", {"year": last_year, "period": "Q1"})
    except FMPError as e:
        raise SystemExit(
            f"Bulk verify FAILED ({e}).\n"
            f"  -> Endpoint/params may differ on your plan, or bulk needs a higher tier.\n"
            f"  -> Check FMP bulk docs, or use scripts/fmp_grab_fundamentals.py (per-ticker)."
        ) from e


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-grab FMP fundamentals to disk.")
    parser.add_argument("--start-year", type=int, default=1996)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--sleep", type=float, default=0.1)
    args = parser.parse_args()

    print("Verifying a bulk endpoint on your tier...")
    _verify()
    print("  OK — bulk endpoint reachable. Starting grab.")

    years = range(args.start_year, args.end_year + 1)
    ok = cached = 0
    errors: list[tuple[str, str]] = []
    for endpoint in STATEMENT_ENDPOINTS:
        for year in years:
            for period in QUARTERS:
                fname = f"{endpoint}_{year}_{period}.csv"
                try:
                    result = _fetch(endpoint, {"year": year, "period": period}, fname)
                    ok += result == "ok"
                    cached += result == "cached"
                    if result == "ok":
                        time.sleep(args.sleep)
                except FMPError as e:
                    errors.append((fname, str(e)[:80]))

    print("\n" + "=" * 56)
    print(f"Bulk fundamentals grab: {ok} fetched, {cached} cached, {len(errors)} errors")
    for fname, msg in errors[:15]:
        print(f"  - {fname}: {msg}")
    print("=" * 56)


if __name__ == "__main__":
    main()
