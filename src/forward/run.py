"""
Forward paper-trading entrypoint — one weekly session, end to end.

Ties the whole forward stack into a single command:
  refresh prices (+news) -> build the decision provider (fundamentals + technicals +
  sentiment + news) -> run_forward_session (screen -> decide -> size via the risk
  layer -> execute against the persistent PaperBook) -> save.

Prices + Benzinga news come from a running IB Gateway/TWS (the only data source —
fundamentals come from EDGAR, free). Needs the Gateway up + a market-data
subscription; the connect/refresh must be validated live (CI has none).

A scheduler (cron/launchd) runs this weekly. The cache-derived helpers
(current_price_from_cache, volatility_from_cache, dividends_since) are unit-tested.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date, timedelta
from functools import partial
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from src.agents.scaffold import ScaffoldConfig
from src.agents.sentiment_specialist import FilingSummaryCache
from src.backtest.batch import BatchLLM
from src.backtest.budget import BudgetedMessages, BudgetMeter
from src.backtest.cache import SignalCache
from src.backtest.costs import CostModel
from src.backtest.screen import select_candidates
from src.data.dividends import load_dividends
from src.data.fetch_prices import load_latest_cache
from src.data.filings import recent_filing_dates
from src.data.fundamentals import get_fundamentals_as_of
from src.data.technicals import get_technicals_as_of
from src.data.universe import nasdaq100_as_of
from src.forward.decide import run_forward_decision
from src.forward.ibkr import (
    FORWARD_PRICE_PERIOD,
    assert_paper_ready,
    connect,
    fetch_news_for,
    news_source_from_store,
    refresh_price_cache,
)
from src.forward.ibkr_execution import account_state, broker_rebalance
from src.forward.notify import (
    heartbeat_stale,
    notify,
    read_heartbeat,
    write_heartbeat,
)
from src.forward.paper import DEFAULT_BOOK_PATH, PaperBook
from src.forward.report import session_digest, write_digest
from src.forward.session import produce_decisions, run_forward_session
from src.risk import RiskConfig, annualized_volatility

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCREEN_LOOKBACK_DAYS = 7  # trailing window for the filing-event screen
SIGNAL_CACHE_DIR = Path("data/cache/signals")
SUMMARY_CACHE_DIR = Path("data/cache/filing_summaries")
# Equity history for the broker path's drawdown governor (real NetLiquidation per run).
# The sim path uses the PaperBook's curve; the broker account is the source of truth here.
BROKER_EQUITY_PATH = Path("data/forward/broker_equity.json")


# ==========================================
# CACHE-DERIVED HELPERS (unit-tested)
# ==========================================


def current_price_from_cache(period: str = FORWARD_PRICE_PERIOD):
    """Latest cached close for a ticker (the IBKR refresh writes under `period`)."""
    def price(ticker: str) -> float | None:
        prices = load_latest_cache(ticker, period) or {}
        if not prices:
            return None
        return float(prices[max(prices)]["close"])
    return price


def volatility_from_cache(period: str, as_of: str, lookback: int):
    """Annualized vol from the cached series as of the date (for risk sizing)."""
    def vol(ticker: str) -> float | None:
        return annualized_volatility(load_latest_cache(ticker, period) or {}, as_of, lookback)
    return vol


def dividends_since(tickers: list[str], last_run_date: str | None, as_of: str) -> dict[str, float]:
    """Per-share dividends with an ex-date in (last_run_date, as_of] — i.e. everything
    since the previous session — so a weekly cadence still credits inter-session
    dividends. With no prior run, only the as_of date's dividends are credited."""
    lo = last_run_date or _prev_day(as_of)
    out: dict[str, float] = {}
    for t in tickers:
        total = sum(div for ex, div in load_dividends(t).items() if lo < ex <= as_of)
        if total:
            out[t] = total
    return out


def _prev_day(d: str) -> str:
    y, m, day = (int(x) for x in d.split("-"))
    return (date(y, m, day) - timedelta(days=1)).isoformat()


def _build_provider(period, news_source, include_news, use_batch, max_llm_usd):
    """Build the budget-capped, optionally-batched decision provider (fundamentals +
    technicals + sentiment + news). Shared by the simulated and broker sessions so both
    decide identically — they differ only in how the resulting decisions are executed."""
    client = AsyncAnthropic()
    mc = BatchLLM(client) if use_batch else client.messages
    if max_llm_usd is not None:
        meter = BudgetMeter(max_llm_usd, batch_discount=0.5 if use_batch else 1.0)
        mc = BudgetedMessages(mc, meter)
    return partial(
        run_forward_decision, mc,
        fundamentals_loader=partial(get_fundamentals_as_of, price_period=period),
        technicals_loader=partial(get_technicals_as_of, price_period=period),
        news_source=news_source,
        include_news=include_news,
        signal_cache=SignalCache(SIGNAL_CACHE_DIR),
        summary_cache=FilingSummaryCache(SUMMARY_CACHE_DIR),
        scaffold_config=ScaffoldConfig(max_concurrent_chunks=10_000) if use_batch else None,
    )


def _load_equity_curve(path: Path = BROKER_EQUITY_PATH) -> list[dict]:
    """Persisted real-account equity history (for the broker drawdown governor)."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — a corrupt log shouldn't abort a run
        logger.warning(f"equity curve read failed: {e}")
        return []


def _append_equity(as_of: str, equity: float, cash: float,
                   path: Path = BROKER_EQUITY_PATH) -> None:
    """Append this run's real NetLiquidation to the equity history."""
    curve = _load_equity_curve(path)
    curve.append({"date": as_of, "cash": cash,
                  "positions_value": equity - cash, "total_value": equity})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(curve, indent=2), encoding="utf-8")


# ==========================================
# ONE FORWARD SESSION
# ==========================================


async def run_forward_paper_session(
    tickers: list[str],
    as_of: str,
    *,
    include_news: bool = True,
    use_batch: bool = True,
    use_risk: bool = True,
    max_llm_usd: float | None = None,
    refresh: bool = True,
    cost_model: CostModel | None = None,
    book_path: Path = DEFAULT_BOOK_PATH,
) -> dict:
    """Run one weekly forward paper session and persist the book.

    Prices + news come from a running IB Gateway/TWS. `refresh=False` skips the pull
    and runs against the warm cache.
    """
    period = FORWARD_PRICE_PERIOD
    book = PaperBook.load(book_path)

    # Screen FIRST (filing dates, not prices), then refresh only the names we'll
    # actually analyze — candidates + holdings, a handful — instead of the whole
    # universe. Keeps the per-session data pull small and under IBKR's historical-
    # request pacing limit.
    held = list(book.positions)
    window_start = (date.fromisoformat(as_of) - timedelta(days=SCREEN_LOOKBACK_DAYS)).isoformat()
    candidates = select_candidates(tickers, held, window_start, as_of, recent_filing_dates)
    logger.info(f"screen: {len(candidates)} candidates "
                f"({len(tickers)} universe + {len(held)} held)")

    news_store: dict[str, list[dict]] = {}
    if refresh:
        ib = await connect()
        try:
            assert_paper_ready(ib)  # preflight: fail fast before any LLM spend
            await refresh_price_cache(ib, candidates, period=period)
            if include_news:
                news_store = await fetch_news_for(ib, candidates, as_of)
        finally:
            ib.disconnect()

    news_source = news_source_from_store(news_store if include_news else {})
    provider = _build_provider(period, news_source, include_news, use_batch, max_llm_usd)

    risk_config = RiskConfig() if use_risk else None
    summary = await run_forward_session(
        book, as_of, tickers,
        candidates=candidates,
        decision_provider=provider,
        current_price=current_price_from_cache(period),
        volatility=(volatility_from_cache(period, as_of, RiskConfig().vol_lookback)
                    if use_risk else None),
        filing_dates_fn=recent_filing_dates,
        cost_model=cost_model or CostModel.ibkr_singapore_fixed(),
        dividends=dividends_since(list(book.positions), book.last_run_date, as_of),
        risk_config=risk_config,
    )
    book.save(book_path)
    digest = session_digest(book, summary)
    logger.info("session digest -> %s\n%s", write_digest(as_of, digest), digest)
    return summary


def _broker_digest(as_of: str, result: dict, decided: int, candidates: int,
                   skipped: int, counts: dict) -> str:
    """Terse digest for a broker session (no PaperBook — the account is the truth)."""
    head = (f"# Forward BROKER session — {as_of}\n\n"
            f"- account {result['account']} | equity ${result['equity']:,.2f} | "
            f"transmit={result['transmit']}\n"
            f"- screened {candidates} → decided {decided}, skipped {skipped}\n"
            f"- directions: {counts}\n\n## Orders\n")
    plans = result.get("plans") or []
    body = "\n".join(f"- {side} {qty} {tk}" for side, qty, tk in plans) or "- none"
    return head + body + "\n"


async def run_forward_broker_session(
    tickers: list[str],
    as_of: str,
    *,
    transmit: bool = False,
    include_news: bool = True,
    use_batch: bool = True,
    use_risk: bool = True,
    max_llm_usd: float | None = None,
    cost_model: CostModel | None = None,
) -> dict:
    """One forward session that executes against the IBKR **paper** account instead of
    the simulated book. Reads real positions/equity, screens (universe filers ∪ held in
    the account), decides with the same pipeline, then sizes + places whole-share orders
    via the risk layer. transmit=False previews (logs orders, places nothing); transmit=
    True requires Gateway's Read-Only API OFF. The paper-account guard is always enforced."""
    period = FORWARD_PRICE_PERIOD
    ib = await connect()
    try:
        acct = assert_paper_ready(ib)          # refuses anything but a paper account
        state = account_state(ib)
        held = list(state.positions)
        window_start = (date.fromisoformat(as_of)
                        - timedelta(days=SCREEN_LOOKBACK_DAYS)).isoformat()
        candidates = select_candidates(tickers, held, window_start, as_of, recent_filing_dates)
        logger.info(f"broker screen: {len(candidates)} candidates ({len(tickers)} universe "
                    f"+ {len(held)} held in {acct}); transmit={transmit}")

        await refresh_price_cache(ib, candidates, period=period)
        news_store = await fetch_news_for(ib, candidates, as_of) if include_news else {}
        news_source = news_source_from_store(news_store)
        provider = _build_provider(period, news_source, include_news, use_batch, max_llm_usd)

        decisions, skipped = await produce_decisions(candidates, as_of, provider)

        price_fn = current_price_from_cache(period)
        wanted = {d.ticker for d in decisions} | set(held)
        prices = {t: price_fn(t) for t in wanted}
        prices = {t: p for t, p in prices.items() if p is not None}

        risk_config = RiskConfig() if use_risk else None
        vols = None
        if risk_config is not None:
            vfn = volatility_from_cache(period, as_of, RiskConfig().vol_lookback)
            vols = {d.ticker: vfn(d.ticker) for d in decisions if d.direction == "BUY"}

        result = await broker_rebalance(
            ib, decisions, prices,
            vols=vols, risk_config=risk_config, equity_curve=_load_equity_curve(),
            cost_model=cost_model or CostModel.ibkr_singapore_fixed(), transmit=transmit,
        )
    finally:
        ib.disconnect()

    _append_equity(as_of, result["equity"], state.cash)
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.direction] = counts.get(d.direction, 0) + 1
    summary = {
        "as_of": as_of, "account": result["account"], "equity": result["equity"],
        "transmit": result["transmit"], "candidates": len(candidates),
        "decided": len(decisions), "skipped": skipped, "directions": counts,
        "plans": result["plans"], "results": result["results"],
    }
    digest = _broker_digest(as_of, result, len(decisions), len(candidates), len(skipped), counts)
    logger.info("broker session %s -> %s\n%s", as_of, write_digest(as_of, digest), digest)
    return summary


def _summary_line(summary: dict) -> str:
    """A compact one-line status for the alert / heartbeat note (sim or broker)."""
    try:
        base = (f"as_of {summary['as_of']} | decided {summary.get('decided')}/"
                f"{summary.get('candidates')} | equity ${summary.get('equity', 0):,.0f} "
                f"| {summary.get('directions')}")
        if "transmit" in summary:  # broker session
            n_orders = len(summary.get("plans", []))
            return base + f" | transmit={summary['transmit']} | orders={n_orders}"
        if "open_positions" in summary:  # sim session
            return base + f" | open {summary['open_positions']}"
        return base
    except Exception:  # noqa: BLE001 — never let formatting break the wrap-up
        return str(summary)[:300]


def _run_healthcheck(max_age_hours: float) -> None:
    """Standalone watchdog (for a separate, more frequent cron): emails if the clock
    has gone quiet — no heartbeat, last run errored, or older than max_age_hours.
    Exits non-zero when stale so the scheduler also records it."""
    hb = read_heartbeat()
    if heartbeat_stale(hb, max_age_hours):
        body = f"forward clock looks stale (>{max_age_hours}h or errored). Last: {hb}"
        logger.warning(body)
        notify("⚠️ parley forward clock is quiet", body)
        raise SystemExit(1)
    logger.info(f"heartbeat OK: {hb}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one weekly forward paper-trading session.")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Universe (default: current Nasdaq-100).")
    parser.add_argument("--as-of", default=date.today().isoformat(),
                        help="Decision date (default today).")
    parser.add_argument("--book", default=None,
                        help="PaperBook path (default data/forward/paper_book.json).")
    parser.add_argument("--no-news", action="store_true", help="Disable the news specialist.")
    parser.add_argument("--no-risk", action="store_true",
                        help="Use flat sizing instead of the risk layer.")
    parser.add_argument("--no-batch", action="store_true",
                        help="Inline LLM calls instead of the Batch API.")
    parser.add_argument("--max-llm-usd", type=float, default=None,
                        help="Hard LLM spend cap for the session.")
    parser.add_argument("--no-refresh", action="store_true",
                        help="Skip the IBKR pull (use the warm cache). Sim execution only.")
    parser.add_argument("--execute", choices=["sim", "ibkr"], default="sim",
                        help="sim = simulated PaperBook (default); ibkr = place orders on "
                             "the IBKR paper account.")
    parser.add_argument("--transmit", action="store_true",
                        help="ibkr only: actually send orders (needs Gateway Read-Only API "
                             "OFF). Without it, orders are previewed (logged), never sent.")
    parser.add_argument("--healthcheck", action="store_true",
                        help="Watchdog mode: email if the last run is stale/errored, then exit.")
    parser.add_argument("--heartbeat-max-hours", type=float, default=24 * 8,
                        help="Healthcheck staleness threshold in hours (default 8 days).")
    args = parser.parse_args()

    if args.healthcheck:
        _run_healthcheck(args.heartbeat_max_hours)
        return

    tickers = args.tickers or nasdaq100_as_of(args.as_of)
    logger.info(f"forward session as_of={args.as_of} | execute={args.execute}"
                f"{' transmit' if args.transmit else ''} | {len(tickers)} tickers | "
                f"news={not args.no_news} risk={not args.no_risk} batch={not args.no_batch}")
    try:
        if args.execute == "ibkr":
            summary = asyncio.run(run_forward_broker_session(
                tickers, args.as_of, transmit=args.transmit,
                include_news=not args.no_news, use_risk=not args.no_risk,
                use_batch=not args.no_batch, max_llm_usd=args.max_llm_usd,
            ))
        else:
            summary = asyncio.run(run_forward_paper_session(
                tickers, args.as_of,
                include_news=not args.no_news, use_risk=not args.no_risk,
                use_batch=not args.no_batch, max_llm_usd=args.max_llm_usd,
                refresh=not args.no_refresh,
                book_path=Path(args.book) if args.book else DEFAULT_BOOK_PATH,
            ))
    except Exception as e:  # noqa: BLE001 — record + alert, then re-raise for a non-zero exit
        body = f"{type(e).__name__}: {e}"
        logger.exception("forward session failed")
        write_heartbeat("error", args.as_of, body)
        notify(f"❌ parley forward FAILED ({args.as_of})", body)
        raise

    note = _summary_line(summary)
    write_heartbeat("ok", args.as_of, note)
    notify(f"✅ parley forward OK ({args.as_of})", note)
    print(summary)


if __name__ == "__main__":
    main()
