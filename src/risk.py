"""
Risk-management layer — the component between synthesis and execution that owns
capital preservation. It *constrains*; it does not vote (productization.md Phase 1).

Today sizing is a naive `confidence × base_pct`. This replaces it with **risk-based**
sizing and portfolio-level guards, all pure/deterministic so it's identical in the
backtest and forward paper paths and fully testable offline:

- **Volatility-targeting (inverse-vol):** each position targets a small annualized
  vol *contribution* (`per_position_vol_target`), so weight = target / σ_asset —
  riskier names get smaller weights, calmer names larger, up to a hard cap. This is
  the "size by risk, not raw confidence" principle; confidence enters only as an
  optional *tilt* within the risk budget, never as the sole sizer.
- **Hard per-position cap** regardless of confidence or how low the vol is.
- **Max gross exposure:** the book is scaled down pro-rata if desired weights sum
  past the cap (1.0 = fully invested, no leverage).
- **Drawdown governor / kill switch:** new risk is tapered to zero as drawdown runs
  from a soft threshold to a hard one.

Long-only for now (the system trades long BUY/SELL). Long/short and sector/factor
concentration limits are clean extensions (the latter needs a sector map we don't
have yet) — noted, not built.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    per_position_vol_target: float = 0.03   # annualized vol each position aims to contribute (3%)
    max_position_pct: float = 0.15          # hard per-name cap (fraction of equity)
    min_position_pct: float = 0.02          # below this, don't bother opening
    max_gross_exposure: float = 1.00        # sum of weights cap (1.0 = no leverage)
    max_sector_pct: float = 0.40            # max aggregate weight in any one sector
    use_confidence_tilt: bool = True        # scale weight by decision confidence
    vol_floor: float = 0.05                 # σ floor; stops low-vol names blowing up weight
    vol_lookback: int = 60                  # trading days of returns for the vol estimate
    drawdown_soft: float = -0.10            # start de-risking past this peak-to-trough drawdown
    drawdown_hard: float = -0.20            # zero new risk at/past this (kill switch)


def annualized_volatility(
    prices: dict[str, dict], as_of: str | None = None,
    lookback: int = 60, periods_per_year: int = 252,
) -> float | None:
    """Annualized volatility of daily simple returns over the trailing `lookback` days
    (using closes on/before `as_of`). None if there isn't enough history."""
    dates = sorted(d for d in prices if as_of is None or d <= as_of)
    if len(dates) < 3:  # need >=2 returns to estimate a stdev
        return None
    window = dates[-(lookback + 1):]  # last lookback+1 closes (or all, if fewer)
    closes = [float(prices[d]["close"]) for d in window]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def drawdown_derisk_multiplier(equity_curve: list[float], cfg: RiskConfig) -> float:
    """A [0, 1] multiplier on new risk based on current peak-to-trough drawdown:
    1.0 above the soft threshold, linearly to 0.0 at the hard (kill) threshold."""
    if not equity_curve:
        return 1.0
    peak = max(equity_curve)
    if peak <= 0:
        return 1.0
    drawdown = (equity_curve[-1] - peak) / peak  # <= 0
    if drawdown >= cfg.drawdown_soft:
        return 1.0
    if drawdown <= cfg.drawdown_hard:
        return 0.0
    # linear taper between soft and hard
    return (drawdown - cfg.drawdown_hard) / (cfg.drawdown_soft - cfg.drawdown_hard)


def target_weight(asset_vol: float | None, confidence: float, cfg: RiskConfig) -> float:
    """Risk-based target weight for one position (fraction of equity), before
    portfolio-level scaling. 0.0 if it can't/shouldn't be sized."""
    if asset_vol is None:
        return 0.0
    vol = max(asset_vol, cfg.vol_floor)
    weight = cfg.per_position_vol_target / vol         # inverse-vol
    if cfg.use_confidence_tilt:
        weight *= confidence
    return min(weight, cfg.max_position_pct)            # hard cap


def size_positions(
    candidates: dict[str, float],            # ticker -> decision confidence
    vols: dict[str, float | None],           # ticker -> annualized vol (or None)
    cfg: RiskConfig,
    derisk_multiplier: float = 1.0,
    sectors: dict[str, str] | None = None,
) -> dict[str, float]:
    """Portfolio-level sizing: risk-based per-name weights, the drawdown governor, the
    per-name floor, the per-sector concentration cap, and the max-gross cap (pro-rata
    scale-down). Returns {ticker: weight} for names worth opening. `sectors` maps
    ticker -> sector; names with no known sector are exempt from the sector cap."""
    raw = {}
    for ticker, conf in candidates.items():
        w = target_weight(vols.get(ticker), conf, cfg) * derisk_multiplier
        if w >= cfg.min_position_pct:
            raw[ticker] = w
    # Sector concentration cap: scale down any sector whose aggregate weight exceeds
    # cfg.max_sector_pct, so the per-name cap can't add up to (e.g.) a 60%-semis book.
    if sectors:
        by_sector: dict[str, list[str]] = {}
        for t in raw:
            sec = sectors.get(t)
            if sec:
                by_sector.setdefault(sec, []).append(t)
        for names in by_sector.values():
            sec_sum = sum(raw[t] for t in names)
            if sec_sum > cfg.max_sector_pct and sec_sum > 0:
                scale = cfg.max_sector_pct / sec_sum
                for t in names:
                    raw[t] *= scale
    gross = sum(raw.values())
    if gross > cfg.max_gross_exposure and gross > 0:
        scale = cfg.max_gross_exposure / gross
        raw = {t: w * scale for t, w in raw.items()}
    return raw
