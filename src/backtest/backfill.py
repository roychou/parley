"""
Staggered data backfill for a full (S&P 500-scale) backtest.

Seeds the on-disk caches the backtest reads from: prices (FMP), point-in-time
fundamentals (EDGAR companyfacts), and filing dates (EDGAR submissions). Every
fetcher is check-first cached, so this runner is idempotent and **resumable** —
re-run it and already-cached tickers cost zero API.

Rate-limit reality:
- FMP free prices are the only *daily-capped* resource (250/day). The runner
  gates price fetches at `fmp_daily_cap` and reports how many remain; run it again
  the next day to finish. ~500 names => a couple of days.
- EDGAR has no daily cap (10 req/s); it throttles politely and finishes in one go.

Run:  uv run python -m src.backtest.backfill --as-of 2026-01-14
(defaults to the current S&P 500). Needs EDGAR_USER_AGENT and FMP_API_KEY set.

Note: EDGAR companyfacts blobs are ~5MB each; ~500 names is a few GB of local
cache (gitignored).
"""
from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass
class BackfillSummary:
    total: int = 0
    prices_fetched: int = 0
    prices_cached: int = 0
    prices_deferred: int = 0  # skipped because the FMP daily cap was reached
    fundamentals_ok: int = 0
    submissions_ok: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def fmp_cap_reached(self) -> bool:
        return self.prices_deferred > 0


# Injectable seam (defaults wired to the real data layer; tests pass stubs).
PriceCachedFn = Callable[[str], bool]
FetchFn = Callable[[str], object]


def run_backfill(
    tickers: list[str],
    *,
    price_is_cached: PriceCachedFn,
    fetch_prices: FetchFn,
    fetch_fundamentals: FetchFn,
    fetch_submissions: FetchFn,
    fmp_daily_cap: int = 240,
    do_prices: bool = True,
    do_fundamentals: bool = True,
    do_submissions: bool = True,
    edgar_sleep_s: float = 0.0,
    fmp_sleep_s: float = 0.0,
) -> BackfillSummary:
    """Seed caches for `tickers`, gating FMP price fetches at the daily cap.

    EDGAR (fundamentals + submissions) has no daily cap and is always attempted.
    Price fetches stop once `fmp_daily_cap` new fetches happen this run; the rest
    are counted as deferred (pick them up on the next run).
    """
    s = BackfillSummary(total=len(tickers))
    for ticker in tickers:
        if do_fundamentals:
            try:
                fetch_fundamentals(ticker)
                s.fundamentals_ok += 1
                if edgar_sleep_s:
                    time.sleep(edgar_sleep_s)
            except Exception as e:  # noqa: BLE001 - per-ticker isolation; one bad name shouldn't halt the run
                s.errors.append((ticker, f"fundamentals: {e}"))

        if do_submissions:
            try:
                fetch_submissions(ticker)
                s.submissions_ok += 1
                if edgar_sleep_s:
                    time.sleep(edgar_sleep_s)
            except Exception as e:  # noqa: BLE001
                s.errors.append((ticker, f"submissions: {e}"))

        if do_prices:
            if price_is_cached(ticker):
                s.prices_cached += 1
            elif s.prices_fetched >= fmp_daily_cap:
                s.prices_deferred += 1
            else:
                try:
                    fetch_prices(ticker)
                    s.prices_fetched += 1
                    if fmp_sleep_s:
                        time.sleep(fmp_sleep_s)
                except Exception as e:  # noqa: BLE001
                    s.errors.append((ticker, f"prices: {e}"))
    return s


# ==========================================
# DEFAULT WIRING (real data layer)
# ==========================================


def _default_runner(
    tickers: list[str],
    price_period: str,
    fmp_daily_cap: int,
    do_prices: bool,
    do_fundamentals: bool,
    do_submissions: bool,
) -> BackfillSummary:
    from src.data.edgar import recent_filing_dates
    from src.data.fetch_prices import get_prices, load_latest_cache
    from src.data.fundamentals import get_filings_history

    return run_backfill(
        tickers,
        price_is_cached=lambda t: load_latest_cache(t, price_period) is not None,
        fetch_prices=lambda t: get_prices(t, period=price_period),
        fetch_fundamentals=get_filings_history,
        fetch_submissions=recent_filing_dates,
        fmp_daily_cap=fmp_daily_cap,
        do_prices=do_prices,
        do_fundamentals=do_fundamentals,
        do_submissions=do_submissions,
        edgar_sleep_s=0.15,  # < 10 req/s, polite to SEC
        fmp_sleep_s=0.3,
    )


# ==========================================
# CLI
# ==========================================


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Backfill caches for a full backtest.")
    parser.add_argument("--as-of", default=None, help="S&P 500 date (YYYY-MM-DD); default current.")
    parser.add_argument("--tickers", nargs="+", default=None, help="Explicit tickers.")
    parser.add_argument("--price-period", default="5y", choices=["1y", "5y"])
    parser.add_argument("--fmp-daily-cap", type=int, default=240)
    parser.add_argument("--skip-prices", action="store_true")
    parser.add_argument("--skip-fundamentals", action="store_true")
    parser.add_argument("--skip-submissions", action="store_true")
    args = parser.parse_args()

    if args.tickers:
        tickers = args.tickers
    else:
        from src.data.universe import current_sp500, sp500_as_of
        tickers = sp500_as_of(args.as_of) if args.as_of else current_sp500()

    logger.info(f"Backfilling {len(tickers)} tickers (price_period={args.price_period}, "
                f"fmp_cap={args.fmp_daily_cap})...")
    s = _default_runner(
        tickers, args.price_period, args.fmp_daily_cap,
        do_prices=not args.skip_prices,
        do_fundamentals=not args.skip_fundamentals,
        do_submissions=not args.skip_submissions,
    )

    print("\n" + "=" * 60)
    print("BACKFILL SUMMARY")
    print("=" * 60)
    print(f"  tickers:        {s.total}")
    print(
        f"  prices fetched: {s.prices_fetched}  "
        f"(cached: {s.prices_cached}, deferred: {s.prices_deferred})"
    )
    print(f"  fundamentals:   {s.fundamentals_ok} ok")
    print(f"  submissions:    {s.submissions_ok} ok")
    print(f"  errors:         {len(s.errors)}")
    for ticker, msg in s.errors[:15]:
        print(f"    - {ticker}: {msg}")
    if s.fmp_cap_reached:
        print(f"\n  FMP daily cap hit — {s.prices_deferred} prices remain. Re-run to resume.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
