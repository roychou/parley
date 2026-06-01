"""Forward session runner: screen -> decide -> execute -> persist, across sessions."""
import pytest

from src.forward.paper import PaperBook
from src.forward.session import run_forward_session
from src.risk import RiskConfig
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis

_R = "Synthetic reasoning padded to satisfy the min_length=50 constraint on the signal/decision."


def _decision(ticker, direction, conf=0.8):
    sig = FundamentalsAnalysis(
        specialist="fundamentals", ticker=ticker,
        signal="BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else "NEUTRAL",
        confidence=conf, reasoning=_R, as_of="2026-06-05", rev_growth_yoy=0.1, pe_ratio=18.0,
        profit_margin=0.2, debt_to_equity=0.4,
    )
    return Decision(ticker=ticker, direction=direction, confidence=conf, rationale=_R,
                    contributing_signals=[sig], as_of="2026-06-05")


@pytest.mark.asyncio
async def test_session_screens_decides_executes_and_persists(tmp_path):
    book = PaperBook(initial_cash=100_000.0)
    path = tmp_path / "book.json"

    plan = {"AAA": _decision("AAA", "BUY"), "BBB": _decision("BBB", "HOLD")}

    async def provider(ticker, as_of):
        return plan.get(ticker)

    def price(ticker):
        return {"AAA": 100.0, "BBB": 50.0}.get(ticker)

    book.save(path)  # establish path
    summary = await run_forward_session(
        book, "2026-06-05", ["AAA", "BBB"],
        decision_provider=provider, current_price=price, stop_loss_pct=None,
    )
    book.save(path)

    assert summary["decided"] == 2 and summary["directions"] == {"BUY": 1, "HOLD": 1}
    assert "AAA" in book.positions and "BBB" not in book.positions  # BUY opened, HOLD didn't
    # persisted
    reloaded = PaperBook.load(path)
    assert "AAA" in reloaded.positions
    assert reloaded.last_run_date == "2026-06-05"


@pytest.mark.asyncio
async def test_session_skips_undecidable_and_unpriced(tmp_path):
    book = PaperBook(initial_cash=100_000.0)

    async def provider(ticker, as_of):
        return None if ticker == "NODATA" else _decision(ticker, "BUY")

    def price(ticker):
        return None if ticker == "NOPRICE" else 100.0

    summary = await run_forward_session(
        book, "2026-06-05", ["GOOD", "NODATA", "NOPRICE"],
        decision_provider=provider, current_price=price, stop_loss_pct=None,
    )
    # NODATA -> no decision; NOPRICE -> decided but can't be traded (no price)
    assert summary["decided"] == 2  # GOOD + NOPRICE decided
    assert "GOOD" in book.positions
    assert "NOPRICE" not in book.positions  # dropped for lack of price


@pytest.mark.asyncio
async def test_session_uses_filing_screen_when_provided(tmp_path):
    book = PaperBook(initial_cash=100_000.0)

    async def provider(ticker, as_of):
        return _decision(ticker, "BUY")

    def price(ticker):
        return 100.0

    # Only CCC filed in the window; the screen should pick it (∪ holdings, none here).
    def filing_dates_fn(ticker):
        return {"CCC": ["2026-06-03"]}.get(ticker, [])

    summary = await run_forward_session(
        book, "2026-06-05", ["AAA", "BBB", "CCC"],
        decision_provider=provider, current_price=price,
        filing_dates_fn=filing_dates_fn, screen_lookback_days=7, stop_loss_pct=None,
    )
    assert summary["candidates"] == 1  # only CCC screened in
    assert list(book.positions) == ["CCC"]


@pytest.mark.asyncio
async def test_session_uses_risk_layer_when_configured(tmp_path):
    """With a risk_config + injected volatility, BUYs are vol-sized as a set (the
    calmer name gets the larger weight), not flat base_pct×confidence."""
    book = PaperBook(initial_cash=100_000.0)

    async def provider(ticker, as_of):
        return _decision(ticker, "BUY", conf=0.8)

    def price(ticker):
        return 100.0

    vols = {"CALM": 0.15, "VOL": 0.45}

    def volatility(ticker):
        return vols.get(ticker)

    await run_forward_session(
        book, "2026-06-05", ["CALM", "VOL"],
        decision_provider=provider, current_price=price,
        risk_config=RiskConfig(), volatility=volatility, stop_loss_pct=None,
    )
    # both opened; calmer (lower-vol) name holds more dollars (inverse-vol)
    assert "CALM" in book.positions and "VOL" in book.positions
    assert book.positions["CALM"]["dollars_at_entry"] > book.positions["VOL"]["dollars_at_entry"]
