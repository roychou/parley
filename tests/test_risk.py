"""Risk-management layer: vol estimate, sizing, gross cap, drawdown governor."""
import pytest

from src.risk import (
    RiskConfig,
    annualized_volatility,
    drawdown_derisk_multiplier,
    size_positions,
    target_weight,
)


def _prices(closes: list[float]) -> dict[str, dict]:
    return {f"2026-0{1 + i // 28}-{1 + i % 28:02d}": {"close": c} for i, c in enumerate(closes)}


# ---- volatility ----------------------------------------------------------
def test_annualized_vol_flat_series_is_zero():
    assert annualized_volatility(_prices([100.0] * 30)) == pytest.approx(0.0)


def test_annualized_vol_scales_with_sqrt_time():
    # constant +/- 1% daily alternating -> known daily stdev; annualized = daily*sqrt(252)
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    vol = annualized_volatility(_prices(closes), lookback=40)
    assert vol is not None and vol > 0
    # sanity: ~1% daily moves -> annualized roughly 0.01*sqrt(252) ~ 0.16, within a band
    assert 0.10 < vol < 0.25


def test_annualized_vol_insufficient_data_none():
    assert annualized_volatility(_prices([100.0, 101.0])) is None


# ---- target weight (inverse-vol + cap + confidence) ----------------------
def test_target_weight_inverse_vol():
    cfg = RiskConfig(per_position_vol_target=0.03, max_position_pct=0.15,
                     use_confidence_tilt=False, vol_floor=0.05)
    # vol 0.30 -> 0.03/0.30 = 0.10
    assert target_weight(0.30, 1.0, cfg) == pytest.approx(0.10)
    # vol 0.10 -> 0.30 capped to 0.15
    assert target_weight(0.10, 1.0, cfg) == pytest.approx(0.15)


def test_target_weight_vol_floor_prevents_blowup():
    cfg = RiskConfig(per_position_vol_target=0.03, max_position_pct=1.0,
                     use_confidence_tilt=False, vol_floor=0.05)
    # near-zero vol would give a huge weight; the floor bounds the inverse at 0.03/0.05=0.6
    # (cap raised to 1.0 here so the *floor* is what binds, not the per-name cap)
    assert target_weight(0.001, 1.0, cfg) == pytest.approx(0.6)


def test_target_weight_confidence_tilt_and_none_vol():
    cfg = RiskConfig(per_position_vol_target=0.03, max_position_pct=0.5,
                     use_confidence_tilt=True, vol_floor=0.05)
    # 0.03/0.30 = 0.10, * 0.5 confidence = 0.05
    assert target_weight(0.30, 0.5, cfg) == pytest.approx(0.05)
    assert target_weight(None, 0.9, cfg) == 0.0  # no vol -> not sizeable


# ---- drawdown governor ---------------------------------------------------
def test_drawdown_governor_taper():
    cfg = RiskConfig(drawdown_soft=-0.10, drawdown_hard=-0.20)
    assert drawdown_derisk_multiplier([100, 110, 108], cfg) == 1.0       # tiny dd
    assert drawdown_derisk_multiplier([100, 100, 80], cfg) == 0.0        # -20% -> kill
    # peak 100, now 85 -> dd -0.15, halfway between soft/hard -> 0.5
    assert drawdown_derisk_multiplier([100, 85], cfg) == pytest.approx(0.5)
    assert drawdown_derisk_multiplier([], cfg) == 1.0                    # no history


# ---- portfolio-level sizing ---------------------------------------------
def test_size_positions_drops_below_floor_and_respects_cap():
    cfg = RiskConfig(per_position_vol_target=0.03, max_position_pct=0.15,
                     min_position_pct=0.02, max_gross_exposure=1.0, use_confidence_tilt=False)
    # AAA vol 0.30 -> 0.10; BBB vol 2.0 -> 0.015 (< floor, dropped); CCC vol 0.10 -> capped 0.15
    weights = size_positions({"AAA": 1.0, "BBB": 1.0, "CCC": 1.0},
                             {"AAA": 0.30, "BBB": 2.0, "CCC": 0.10}, cfg)
    assert set(weights) == {"AAA", "CCC"}
    assert weights["AAA"] == pytest.approx(0.10) and weights["CCC"] == pytest.approx(0.15)


def test_size_positions_scales_down_to_max_gross():
    cfg = RiskConfig(per_position_vol_target=0.03, max_position_pct=0.50,
                     min_position_pct=0.01, max_gross_exposure=0.50, use_confidence_tilt=False)
    # three names each ~0.30 weight (vol 0.10 -> capped... use vol 0.10 -> 0.30) sum 0.90 > 0.50
    weights = size_positions({"A": 1.0, "B": 1.0, "C": 1.0},
                             {"A": 0.10, "B": 0.10, "C": 0.10}, cfg)
    assert sum(weights.values()) == pytest.approx(0.50)  # scaled to gross cap
    assert all(w == pytest.approx(0.50 / 3) for w in weights.values())  # pro-rata


def test_size_positions_derisk_multiplier_shrinks_all():
    cfg = RiskConfig(per_position_vol_target=0.03, max_position_pct=0.15,
                     min_position_pct=0.01, use_confidence_tilt=False)
    full = size_positions({"AAA": 1.0}, {"AAA": 0.30}, cfg, derisk_multiplier=1.0)
    half = size_positions({"AAA": 1.0}, {"AAA": 0.30}, cfg, derisk_multiplier=0.5)
    assert half["AAA"] == pytest.approx(full["AAA"] * 0.5)
