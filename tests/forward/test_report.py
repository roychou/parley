from src.forward.paper import PaperBook
from src.forward.report import session_digest


def test_session_digest_renders_session():
    book = PaperBook(initial_cash=100_000, cash=88_000, dividends_received=12.5)
    book.positions = {"AAPL": {"ticker": "AAPL", "entry_date": "2026-06-01",
                               "entry_price": 200.0, "dollars_at_entry": 12000.0,
                               "cost_at_entry": 1.0}}
    book.decision_log = [
        {"date": "2026-06-05", "ticker": "AAPL", "direction": "BUY",
         "confidence": 0.7, "rationale": "strong margins and growth"},
        {"date": "2026-05-01", "ticker": "NVDA", "direction": "SELL",
         "confidence": 0.5, "rationale": "prior week"},
    ]
    summary = {"as_of": "2026-06-05", "candidates": 5, "decided": 1,
               "directions": {"BUY": 1}, "skipped": ["FER", "ASML"], "equity": 100012.5}
    d = session_digest(book, summary)
    assert "Forward session — 2026-06-05" in d
    assert "AAPL BUY" in d and "strong margins" in d   # decision + rationale shown
    assert "NVDA" not in d                              # prior-date decision excluded
    assert "skipped 2" in d and "FER" in d              # skips surfaced
    assert "AAPL: $12,000" in d                         # open position
    assert "vs $100,000 start" in d                     # P&L line
