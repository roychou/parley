"""
Grab a ticker -> CIK map for the full historical universe (FMP Premium).

Why: EDGAR fundamentals are keyed by CIK (permanent), but SEC's ticker list is
current-only, so delisted tickers don't resolve. FMP's profile endpoint covers
many delisted names and returns their CIK. Grabbing this map now (perishable
Premium access) preserves the OPTION to pull delisted companies' point-in-time
XBRL fundamentals later via EDGAR companyfacts-by-CIK (free, Release 2 wiring).

Proven viable on SIVB (CIK 0000719739 -> EDGAR EPS 2010->2023). Partial coverage:
some names return no CIK (e.g. ACAS) or a CIK with no companyfacts (e.g. FRC).

    uv run python scripts/fmp_grab_cik_map.py --start 1996-01-01

Resumable: keeps a single JSON map; skips tickers already resolved.
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from src.data.fmp_client import FMPError, _get  # noqa: E402
from src.data.universe import sp500_members_in_range  # noqa: E402

OUT_PATH = Path("data/reference/ticker_cik_historical.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Grab ticker->CIK map (FMP profiles).")
    parser.add_argument("--start", default="1996-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--sleep", type=float, default=0.1)
    args = parser.parse_args()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str | None] = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}

    end = args.end or date.today().isoformat()
    tickers = sp500_members_in_range(args.start, end)
    todo = [t for t in tickers if t not in mapping]
    print(f"Resolving CIK for {len(todo)} tickers ({len(mapping)} already mapped)...")

    resolved = errors = 0
    for i, ticker in enumerate(todo, 1):
        try:
            prof = _get("profile", params={"symbol": ticker})
            if isinstance(prof, list):
                prof = prof[0] if prof else {}
            cik = prof.get("cik") if isinstance(prof, dict) else None
            mapping[ticker] = str(cik).zfill(10) if cik else None
            resolved += cik is not None
            time.sleep(args.sleep)
        except FMPError:
            mapping[ticker] = None
            errors += 1
        if i % 200 == 0:
            OUT_PATH.write_text(json.dumps(mapping, indent=2))
            print(f"  {i}/{len(todo)} (resolved={resolved}, errors={errors})")

    OUT_PATH.write_text(json.dumps(mapping, indent=2))
    have = sum(1 for v in mapping.values() if v)
    print("\n" + "=" * 56)
    print(f"ticker->CIK map: {len(mapping)} tickers, {have} with CIK, {len(mapping) - have} none")
    print(f"  written to {OUT_PATH}")
    print("=" * 56)


if __name__ == "__main__":
    main()
