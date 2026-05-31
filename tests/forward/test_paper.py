"""Forward paper-trading book: persistence round-trip + a forward step."""
import pytest

from src.forward.paper import PaperBook, run_forward_step
from src.schemas import Decision
from src.schemas.fundamentals import FundamentalsAnalysis

_REASON = "Synthetic reasoning padded to satisfy the min_length constraint on the signal/decision."


def _decision(ticker: str, direction: str, confidence: float) -> Decision:
    sig = FundamentalsAnalysis(
        specialist="fundamentals", ticker=ticker,
        signal="BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else "NEUTRAL",
        confidence=confidence, reasoning=_REASON, as_of="2026-02-06",
        rev_growth_yoy=0.1, pe_ratio=18.0, profit_margin=0.2, debt_to_equity=0.4,
    )
    return Decision(
        ticker=ticker, direction=direction, confidence=confidence, rationale=_REASON,
        contributing_signals=[sig], as_of="2026-02-06",
    )


def test_book_persistence_round_trip(tmp_path):
    path = tmp_path / "book.json"
    book = PaperBook(initial_cash=100_000.0)
    p = book.to_portfolio()
    p.open("AAA", "2026-02-06", price=100.0, dollars=10_000.0)
    book.update_from_portfolio(p)
    book.last_run_date = "2026-02-06"
    book.save(path)

    reloaded = PaperBook.load(path)
    assert reloaded.last_run_date == "2026-02-06"
    assert reloaded.cash == p.cash
    p2 = reloaded.to_portfolio()
    assert "AAA" in p2.positions and p2.positions["AAA"].dollars_at_entry == 10_000.0


def test_load_missing_returns_fresh_book(tmp_path):
    book = PaperBook.load(tmp_path / "nope.json")
    assert book.cash == book.initial_cash == 100_000.0
    assert book.positions == {} and book.last_run_date is None


def test_forward_step_opens_on_buy_and_logs(tmp_path):
    book = PaperBook(initial_cash=100_000.0)
    prices = {"AAA": 100.0, "BBB": 50.0}
    decisions = [_decision("AAA", "BUY", 0.8), _decision("BBB", "HOLD", 0.5)]

    run_forward_step(book, "2026-02-06", prices, decisions, stop_loss_pct=None)

    assert "AAA" in book.positions          # BUY opened
    assert "BBB" not in book.positions      # HOLD did nothing
    assert book.last_run_date == "2026-02-06"
    assert [d["ticker"] for d in book.decision_log] == ["AAA", "BBB"]  # full audit trail
    assert book.equity_curve[-1]["date"] == "2026-02-06"


def test_forward_step_sells_then_marks_to_market_across_sessions(tmp_path):
    book = PaperBook(initial_cash=100_000.0)
    # session 1: open AAA at 100
    run_forward_step(book, "2026-02-06", {"AAA": 100.0}, [_decision("AAA", "BUY", 0.8)],
                     stop_loss_pct=None)
    assert "AAA" in book.positions

    # session 2 (no decision): price rises to 110, MTM lifts equity, no trade
    run_forward_step(book, "2026-02-13", {"AAA": 110.0}, decisions=None, stop_loss_pct=None)
    assert book.equity_curve[-1]["total_value"] > 100_000.0

    # session 3: SELL closes the position
    run_forward_step(book, "2026-02-20", {"AAA": 110.0}, [_decision("AAA", "SELL", 0.7)],
                     stop_loss_pct=None)
    assert "AAA" not in book.positions
    assert len(book.closed_trades) == 1


def test_forward_step_credits_dividends():
    book = PaperBook(initial_cash=100_000.0)
    run_forward_step(book, "2026-02-06", {"AAA": 100.0}, [_decision("AAA", "BUY", 0.8)],
                     stop_loss_pct=None)
    # BUY sized by confidence: 0.10 base * 0.8 = 8% -> $8,000 -> 80 shares @ $100.
    # $1/share dividend on a later session -> $80 credited.
    run_forward_step(book, "2026-02-13", {"AAA": 100.0}, decisions=None,
                     dividends={"AAA": 1.0}, stop_loss_pct=None)
    assert book.dividends_received == pytest.approx(80.0)
