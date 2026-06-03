# Quality-Value baseline — design

> Status: design (not yet built). A deterministic factor strategy that joins the
> existing baseline set (SPY-hold, random, RSI, P/E-ranking). **This is a baseline, not
> a deployed strategy** — it never touches the forward/live path and is explicitly
> non-gating per `productization.md` (Phase 5 enhancements stay gated behind GATE 0).

## Why this exists (the role, stated honestly)

1. **An honest bar for the multi-agent line.** The current value baseline (`pe_ranking`)
   is a single-factor toy — lowest-P/E quintile, which famously buys value traps. A
   real quality-value composite is the bar the LLM system should have to clear before
   we believe it adds anything.
2. **A contamination-free signal (the key property).** A factor strategy is *arithmetic
   on point-in-time filings + current price* — there is **no LLM memory in it**, so it
   is valid even on in-training-window dates where the multi-agent line cannot be
   trusted (`productization.md` 0.0 / GATE 0). It's the one strategy line whose backtest
   number means the same thing in-window and out.
3. **(Later) a measurable sleeve.** If the forward clock eventually shows the base
   system has edge, a value *tilt* becomes a Phase-5 candidate — and this baseline is
   exactly what we'd measure its incremental contribution against.

## Design principles (inherited from the existing baselines)

- **Deterministic, pure, offline in the loop.** Reads cached prices + the
  `ValuationSnapshot` per ticker; no LLM, no network. Reproducible bit-for-bit.
- **Point-in-time honest for free.** `ValuationSnapshot` is already anchored to
  `report_date` (the SEC filing date, when the data became public), so ranking on it
  carries no look-ahead. Nothing extra to enforce.
- **Conforms to the `Strategy` Protocol** (`decide_all → list[Action]`), so it plugs
  straight into the replay loop and inherits the cost model, dividend crediting,
  metrics, and alpha/beta regression with zero new plumbing.
- **Cross-sectional, top-basket, equal-weight** — same shape as `PERankingStrategy`,
  just multi-factor.

## What is computable *today* (the binding constraint)

`ValuationSnapshot` (src/data/fundamentals.py) exposes exactly:
`pe_ratio, diluted_eps, profit_margin, rev_growth_yoy, debt_to_equity`.

So the composite must be built from these. Mapping to quality-value:

| Leg | Factor | Source field | Direction |
|---|---|---|---|
| **Value** | earnings yield `1/PE` | `pe_ratio` | higher = cheaper = better |
| **Quality** | profitability | `profit_margin` | higher better |
| **Quality** | balance-sheet safety | `debt_to_equity` | **lower** better |
| *(optional)* | growth | `rev_growth_yoy` | higher better (default weight 0) |

Notable gaps vs. a textbook composite: **no P/B, no FCF yield, no EV/EBITDA, no ROIC**.
Quality is proxied by margin + low leverage rather than return-on-capital. This is a
Greenblatt "Magic Formula"-*lite*: cheap (earnings yield) × good (margin, low debt),
with ROIC unavailable so substituted. Honest, defensible, and shippable with **zero new
data work** — see the extension section for closing the gaps cheaply.

## The composite — rank-sum (default), z-score (alternative)

**Default: rank-sum (Magic Formula construction).** For each factor, rank all *eligible*
tickers cross-sectionally at the rebalance date; the composite score is the sum of
per-factor ranks (oriented so lower = better). Hold the top `N`, equal-weight.

Why rank-sum is the default for a *baseline*: no winsorization, no distributional
assumption, no tuning surface to overfit — it is the most defensible, least-parameterized
choice, which is the whole point of a baseline. Greenblatt's original method is literally
rank(earnings yield) + rank(ROIC); we substitute the quality leg.

```
eligible   = {t : pe_ratio(t) > 0, profit_margin(t) and debt_to_equity(t) present}
rank_value = rank_desc(earnings_yield)      # cheapest = rank 1
rank_marg  = rank_desc(profit_margin)
rank_lev   = rank_asc(debt_to_equity)       # least levered = rank 1
score(t)   = w_v·rank_value + w_q·(rank_marg + rank_lev)/2     # + w_g·rank_growth
basket     = N tickers with the lowest score   # equal-weight 1/N
```

Default weights: **value 0.5, quality 0.5** (margin and leverage split the quality leg
evenly); growth weight 0 (keep it value-quality, not momentum). All configurable on the
constructor so a backtest can sweep them — but the *committed default* is the
unparameterized 50/50.

**Alternative: cross-sectional z-scores** (`composite = Σ wᵢ·z(factorᵢ)`, winsorized at
±3σ, leverage entered as `−d/e`). Keeps magnitude information; standard in quant factor
work. Offer it as a constructor flag (`method="zscore"`) for comparison, but rank-sum
stays the default for the reasons above.

## Eligibility & data hygiene

- Require `pe_ratio > 0` and non-NaN (positive earnings) — identical to `pe_ranking`.
- Require `profit_margin` and `debt_to_equity` non-NaN; otherwise **exclude** from
  ranking (cleaner and more honest than median-imputing a missing factor).
- **Known limitation:** foreign EUR annual filers (ASML/CCEP/FER) have NaN P/E by design
  (no USD EPS — see the EDGAR annual/IFRS work), so they're auto-excluded. USD annual
  filers (PDD/TRI) keep P/E and remain eligible. Acceptable for a baseline; document it.

## Sizing & cadence

- **Equal-weight the basket (`1/N`).** Do *not* route the baseline through the risk
  layer — the multi-agent line is what gets inverse-vol sizing; mixing muddies the
  comparison. (A `--risk` variant is a later option, not the default.)
- Rebalances on the backtest's own period grid, same dates as every other line — so the
  comparison stays apples-to-apples. (Value's natural multi-year horizon vs. a weekly
  grid is a *live-cadence* question, separate from this baseline's role as a comparator.)

## Where it plugs in

- New `QualityValueStrategy` in `src/backtest/strategies.py`, modeled on
  `PERankingStrategy` (constructor: `basket_size`, weights, `method`).
- Add one line to the strategy list in `src/backtest/run.py` (`_strategies()` ~L162).
- Pure ranking helpers extracted so they unit-test in isolation against synthetic
  `ValuationSnapshot`s — no replay needed.

## Tests

- **Unit (pure):** synthetic universe → assert the basket is the cheap-and-clean names;
  NaN / non-positive P/E excluded; missing quality factor excluded; ties stable;
  equal weights sum to ~1; weight knobs move the basket as expected.
- **Backtest comparison:** run alongside the existing baselines and read the standard
  summary (total, Sharpe, maxDD, vs-SPY, alpha/beta). **Hypothesis under test:** the
  quality leg should reduce drawdown vs. pure low-P/E (`pe_ranking`) by screening out the
  value traps low P/E alone buys. If it doesn't, that's a real finding about this universe.

## MVP now vs. EDGAR extension later (the one real fork)

**MVP (everything above):** uses only existing snapshot fields. Zero new data plumbing —
lands as a baseline immediately.

**Extension (EDGAR is free; concepts are available in companyfacts):**
- **Book-to-market (P/B) — cheapest, highest value.** `StockholdersEquity` is *already
  parsed* (it sits behind `debt_to_equity`). Add **shares outstanding**
  (`dei:EntityCommonStockSharesOutstanding`, or us-gaap weighted-avg diluted shares) →
  market cap = `price × shares` → book-to-market = `equity / mktcap`. Adds a second,
  independent value ratio and a proper book yield. One new concept to surface.
- **ROE — better quality leg than margin.** `NetIncomeLoss / StockholdersEquity` (NI is
  already implied by `profit_margin`; surface it). Turns "quality" into return-on-equity,
  the textbook leg.
- **FCF yield.** `NetCashProvidedByUsedInOperatingActivities −
  PaymentsToAcquirePropertyPlantAndEquipment`, / market cap. Robust value signal; two
  more concepts.
- **EV/EBITDA — defer.** Needs debt + cash + an EBITDA reconstruction; most work, least
  marginal value here.

Recommendation: **ship the MVP first**, then a single focused extension adding
**book-to-market + ROE** (proper value + proper quality) — each is additive: one field
on the snapshot + one weight in the composite, no structural change.

## Scope guardrail

Baseline only. Does not modify the LLM/forward/live path. Non-gating. The order in
`productization.md` stands: validate the base system on temporally-valid forward data
*before* enhancing signal. This baseline is comparison infrastructure that serves that
validation — not a step toward deploying a factor strategy.
