"""
Catalyst/signal grab (FMP Premium) — per-symbol, deep history, to local disk.

Grabs the point-in-time-clean datasets the probe confirmed on Premium, each
feeding a consumer:
- earnings   (actual vs estimate, ~1985+)  -> event screen triggers + fundamentals
- dividends  (declaration/record/pay)       -> total-return / adjustment fidelity
- splits                                     -> adjustment correctness
- grades     (analyst upgrades/downgrades)   -> sentiment specialist catalysts

Run during the Premium window (after the price + bulk-fundamentals grabs, to
avoid rate contention), with FMP_API_KEY set:

    uv run python scripts/fmp_grab_signals.py --start 1996-01-01

Resumable (skips cached per ticker+dataset). These endpoints return full history
by default (no limit needed). Stored raw JSON; runtime wiring into the screen /
specialists is a separate step.
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
load_dotenv(_ROOT / ".env")  # robust regardless of cwd

from src.data.fmp_client import FMPError, _get  # noqa: E402
from src.data.universe import sp500_members_in_range  # noqa: E402

OUT_DIR = Path("data/cache/fmp_signals")
# dataset name -> FMP stable endpoint (all per-symbol, full history by default)
ENDPOINTS = {
    "earnings": "earnings",
    "dividends": "dividends",
    "splits": "splits",
    "grades": "grades",
}


def _grab(ticker: str, dataset: str, endpoint: str) -> str:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{ticker}_{dataset}.json"
    if path.exists():
        return "cached"
    data = _get(endpoint, params={"symbol": ticker})
    path.write_text(json.dumps(data))
    return "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description="Grab per-symbol catalyst/signal data.")
    parser.add_argument("--start", default="1996-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--sleep", type=float, default=0.1)
    args = parser.parse_args()

    end = args.end or date.today().isoformat()
    tickers = sp500_members_in_range(args.start, end)
    print(f"Grabbing {list(ENDPOINTS)} for {len(tickers)} tickers...")

    ok = cached = 0
    errors: list[tuple[str, str]] = []
    for i, ticker in enumerate(tickers, 1):
        for dataset, endpoint in ENDPOINTS.items():
            try:
                result = _grab(ticker, dataset, endpoint)
                ok += result == "ok"
                cached += result == "cached"
                if result == "ok":
                    time.sleep(args.sleep)
            except FMPError as e:
                errors.append((f"{ticker}/{dataset}", str(e)[:70]))
        if i % 100 == 0:
            print(f"  {i}/{len(tickers)} (ok={ok}, cached={cached}, errors={len(errors)})")

    print("\n" + "=" * 56)
    print(f"Signal grab: {ok} fetched, {cached} cached, {len(errors)} errors")
    for key, msg in errors[:15]:
        print(f"  - {key}: {msg}")
    print("=" * 56)


if __name__ == "__main__":
    main()
