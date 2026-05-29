from src.backtest.backfill import run_backfill


def _stubs(cached=None, failing_prices=None):
    cached = set(cached or [])
    failing_prices = set(failing_prices or [])
    calls = {"prices": [], "fundamentals": [], "submissions": []}

    def price_is_cached(t):
        return t in cached

    def fetch_prices(t):
        if t in failing_prices:
            raise RuntimeError("boom")
        calls["prices"].append(t)

    def fetch_fundamentals(t):
        calls["fundamentals"].append(t)

    def fetch_submissions(t):
        calls["submissions"].append(t)

    return calls, dict(
        price_is_cached=price_is_cached,
        fetch_prices=fetch_prices,
        fetch_fundamentals=fetch_fundamentals,
        fetch_submissions=fetch_submissions,
    )


def test_fetches_all_when_under_cap():
    calls, fns = _stubs()
    s = run_backfill(["A", "B", "C"], fmp_daily_cap=10, **fns)
    assert s.prices_fetched == 3 and s.prices_deferred == 0
    assert calls["prices"] == ["A", "B", "C"]
    assert s.fundamentals_ok == 3 and s.submissions_ok == 3


def test_defers_prices_past_daily_cap():
    calls, fns = _stubs()
    s = run_backfill(["A", "B", "C", "D", "E"], fmp_daily_cap=2, **fns)
    assert s.prices_fetched == 2
    assert s.prices_deferred == 3
    assert s.fmp_cap_reached is True
    # EDGAR has no cap — all fundamentals/submissions still done.
    assert s.fundamentals_ok == 5 and s.submissions_ok == 5


def test_cached_prices_not_fetched_and_dont_count_against_cap():
    calls, fns = _stubs(cached=["A", "B"])
    s = run_backfill(["A", "B", "C", "D"], fmp_daily_cap=2, **fns)
    assert s.prices_cached == 2
    assert calls["prices"] == ["C", "D"]  # only the uncached ones fetched
    assert s.prices_fetched == 2 and s.prices_deferred == 0


def test_error_isolation_continues_run():
    calls, fns = _stubs(failing_prices=["B"])
    s = run_backfill(["A", "B", "C"], fmp_daily_cap=10, **fns)
    assert calls["prices"] == ["A", "C"]  # B failed but run continued
    assert s.prices_fetched == 2
    assert len(s.errors) == 1 and s.errors[0][0] == "B"


def test_skip_flags():
    calls, fns = _stubs()
    s = run_backfill(["A", "B"], fmp_daily_cap=10, do_prices=False, do_submissions=False, **fns)
    assert calls["prices"] == [] and calls["submissions"] == []
    assert calls["fundamentals"] == ["A", "B"]
    assert s.prices_fetched == 0 and s.fundamentals_ok == 2
