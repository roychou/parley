# IC Validation — contamination-free cross-sectional test (NULL result)

> Derived from the 2026-06-04 ladder run; re-derived deterministically (no LLM) from cached
> per-date convictions. Per-date IC in `ic_by_date.csv`. Regenerate with
> `python -m src.backtest.score_all --model claude-sonnet-4-5-20250929 --sentiment
> --hybrid-sentiment --dates <39 dates> --horizon 20 --spacing 5` (costs LLM; see scripts/run_ladder_sonnet45.sh).

## Result
| metric | value |
|---|---|
| decision model | `claude-sonnet-4-5-20250929` (Sonnet 4.5) |
| training-data cutoff | 2025-07-31 |
| clean window | 2025-08-01 → 2026-04-24 (weekly) |
| n_dates | 39 |
| n_obs (name-dates) | 3212 |
| forward horizon | 20 trading days |
| **mean IC** | **-0.0046** |
| std of per-date IC | 0.1362 |
| **cross-date t-stat** | **-0.2090** (overlap-optimistic) |

## What "clean date" means
A decision date is clean if it is strictly AFTER the decision model's training-data cutoff
(2025-07-31), so the model is judging data it was not trained on. Dates also require 20 trading
days of subsequent prices in the cache to compute a forward return. Older-cutoff models manufacture
a longer clean window than the deployed model (whose cutoff leaves only ~12 weekly clean dates).

## Methodology and conclusion (honest)
Information Coefficient = Spearman rank-correlation between the model's signed conviction and the
forward 20-day return, computed cross-sectionally across the Nasdaq-100 on each date, then averaged
across dates. The full multi-agent tool (fundamentals + technicals + carry-forward hybrid sentiment)
was scored on every covered name each date. Over 39 weekly dates and 3212
name-observations, **mean IC is -0.0046 with a cross-date t-statistic of -0.2090 —
statistically indistinguishable from zero.**

**Conclusion: no detectable cross-sectional ranking edge.** Adding dates pulled the per-date IC
spread down (std 0.14) toward zero rather than revealing a hidden signal. The t-statistic
is overlap-optimistic (weekly decisions with 20-day forward windows overlap), so true significance
is no stronger. This is a NEGATIVE result. It tests cross-sectional name *ranking* only — not risk
management, position sizing, or timing. It is consistent with the project's core thesis: an
in-training-window LLM backtest measures memory, and on clean post-cutoff data this approach shows
no measurable ranking alpha. Forward paper trading remains the only fully clean evaluation.
