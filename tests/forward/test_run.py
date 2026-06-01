"""Forward entrypoint helpers (cache-derived). The IBKR orchestration is live-validated."""
import src.forward.run as fwd


def test_current_price_from_cache_latest_close(monkeypatch):
    series = {"2026-05-20": {"close": 101.2}, "2026-05-22": {"close": 102.7}}
    monkeypatch.setattr(fwd, "load_latest_cache", lambda t, p: series if t == "AAA" else {})
    price = fwd.current_price_from_cache("ibkr")
    assert price("AAA") == 102.7        # latest date's close
    assert price("ZZZ") is None         # no cache -> None


def test_volatility_from_cache(monkeypatch):
    # a varying series -> a positive vol; empty -> None
    series = {f"2026-04-{i + 1:02d}": {"close": 100 + (i % 2) * 3} for i in range(20)}
    monkeypatch.setattr(fwd, "load_latest_cache",
                        lambda t, p: series if t == "AAA" else {})
    vol = fwd.volatility_from_cache("ibkr", "2026-05-01", lookback=60)
    assert vol("AAA") is not None and vol("AAA") > 0
    assert vol("ZZZ") is None


def test_dividends_since_window(monkeypatch):
    divs = {"AAA": {"2026-05-10": 0.5, "2026-05-20": 0.6, "2026-06-01": 0.7}}
    monkeypatch.setattr(fwd, "load_dividends", lambda t: divs.get(t, {}))
    # window (2026-05-15, 2026-05-31]: only the 05-20 dividend counts
    out = fwd.dividends_since(["AAA"], "2026-05-15", "2026-05-31")
    assert out == {"AAA": 0.6}
    # nothing held / no dividends in window -> empty
    assert fwd.dividends_since(["BBB"], "2026-05-15", "2026-05-31") == {}
