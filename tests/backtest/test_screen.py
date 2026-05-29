import pytest

from src.backtest.portfolio import Portfolio
from src.backtest.screen import select_candidates
from src.backtest.strategies import MultiAgentStrategy

# Synthetic filing calendar: ticker -> 10-Q/10-K filing dates.
_CALENDAR = {
    "AAA": ["2024-01-30", "2024-04-30"],
    "BBB": ["2024-02-15"],
    "CCC": ["2023-11-01"],          # filed before the window
    "DDD": [],                       # never filed
}


def _filed(ticker):
    return _CALENDAR.get(ticker, [])


# ==========================================
# select_candidates (pure)
# ==========================================


def test_selects_only_window_filers():
    # Window (2024-01-15, 2024-03-01]: AAA (01-30) and BBB (02-15) qualify; CCC/DDD don't.
    got = select_candidates(["AAA", "BBB", "CCC", "DDD"], [], "2024-01-15", "2024-03-01", _filed)
    assert got == ["AAA", "BBB"]


def test_window_start_exclusive_end_inclusive():
    # A filing exactly on window_start is excluded; exactly on window_end is included.
    cal = {"X": ["2024-01-15"], "Y": ["2024-03-01"]}
    got = select_candidates(["X", "Y"], [], "2024-01-15", "2024-03-01", lambda t: cal.get(t, []))
    assert got == ["Y"]


def test_held_always_included_even_if_no_filing():
    # DDD never filed and CCC filed outside the window, but both are held → kept.
    got = select_candidates(["AAA"], ["CCC", "DDD"], "2024-01-15", "2024-03-01", _filed)
    assert got == ["AAA", "CCC", "DDD"]


def test_held_outside_eligible_universe_still_kept():
    # A holding that left the index (not in eligible) must remain analyzable (to sell).
    got = select_candidates(["AAA"], ["ZZZ"], "2024-01-15", "2024-03-01", _filed)
    assert "ZZZ" in got


# ==========================================
# MultiAgentStrategy screening integration
# ==========================================


@pytest.mark.asyncio
async def test_strategy_screens_universe_by_filing(monkeypatch):
    analyzed: list[str] = []

    async def provider(ticker, date):
        analyzed.append(ticker)
        from src.schemas import Decision
        from src.schemas.fundamentals import FundamentalsAnalysis
        sig = FundamentalsAnalysis(
            specialist="fundamentals", ticker=ticker, signal="NEUTRAL", confidence=0.5,
            reasoning="x" * 60, as_of=date, rev_growth_yoy=0.1, pe_ratio=20.0,
            profit_margin=0.2, debt_to_equity=0.4,
        )
        return Decision(ticker=ticker, direction="HOLD", confidence=0.5,
                        rationale="y" * 60, contributing_signals=[sig], as_of=date)

    strat = MultiAgentStrategy(decision_provider=provider, filing_dates_fn=_filed)
    portfolio = Portfolio()
    # First call uses a lookback window ending on the date; only AAA/BBB filed in early 2024.
    await strat.decide_all(["AAA", "BBB", "CCC", "DDD"], "2024-03-01", {}, {}, portfolio)
    assert sorted(analyzed) == ["AAA", "BBB"]


@pytest.mark.asyncio
async def test_strategy_without_filing_fn_analyzes_all(monkeypatch):
    analyzed: list[str] = []

    async def provider(ticker, date):
        analyzed.append(ticker)
        from src.schemas import Decision
        from src.schemas.fundamentals import FundamentalsAnalysis
        sig = FundamentalsAnalysis(
            specialist="fundamentals", ticker=ticker, signal="NEUTRAL", confidence=0.5,
            reasoning="x" * 60, as_of=date, rev_growth_yoy=0.1, pe_ratio=20.0,
            profit_margin=0.2, debt_to_equity=0.4,
        )
        return Decision(ticker=ticker, direction="HOLD", confidence=0.5,
                        rationale="y" * 60, contributing_signals=[sig], as_of=date)

    strat = MultiAgentStrategy(decision_provider=provider)  # no screen
    await strat.decide_all(["AAA", "BBB", "CCC", "DDD"], "2024-03-01", {}, {}, Portfolio())
    assert sorted(analyzed) == ["AAA", "BBB", "CCC", "DDD"]
