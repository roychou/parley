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


def test_summary_line_broker_vs_sim():
    broker = {"as_of": "2026-06-08", "decided": 6, "candidates": 6, "equity": 1_000_000.0,
              "directions": {"BUY": 3, "HOLD": 3}, "transmit": True,
              "plans": [("BUY", 359, "ROST"), ("BUY", 311, "WMT")]}
    line = fwd._summary_line(broker)
    assert "transmit=True" in line and "orders=2" in line and "$1,000,000" in line

    sim = {"as_of": "2026-06-08", "decided": 4, "candidates": 5, "equity": 100_000.0,
           "directions": {"BUY": 1}, "open_positions": 2}
    assert "open 2" in fwd._summary_line(sim)


def test_equity_curve_roundtrip(tmp_path):
    p = tmp_path / "broker_equity.json"
    assert fwd._load_equity_curve(p) == []                 # absent -> empty
    fwd._append_equity("2026-06-08", 1_000_000.0, 850_000.0, path=p)
    fwd._append_equity("2026-06-15", 1_010_000.0, 860_000.0, path=p)
    curve = fwd._load_equity_curve(p)
    assert [s["total_value"] for s in curve] == [1_000_000.0, 1_010_000.0]
    assert curve[0]["positions_value"] == 150_000.0        # equity - cash
