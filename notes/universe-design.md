# Universe Design — how the bot picks which tickers to run on

> Design note. Captures the decision made while discussing how the bot runs with
> real money. Status: design only, not built. Targets Release 2 (Release 1 ships
> on the curated watchlist, honestly labeled). See `notes/backtest-design.md` for
> the Release 1 backtest scope.

---

## The problem with today's setup

Today `universe` is a static `list[str]` — either a curated ~15-name watchlist
or `DEFAULT_TICKERS` in `src/backtest/run.py` — handed to the bot, which analyzes
every name in it. That single list silently does **three different jobs** that should
be separate stages:

1. **Eligibility** — what the bot is *allowed* to trade (hard, rule-based).
2. **Candidate selection** — what's *worth analyzing* this period (cheap screen).
3. **Analysis** — the deep multi-agent judgment (expensive; the bot).

Collapsing them into "15 names Roy believes in" has three costs:

- **Curation bias.** The bot can only choose among pre-blessed names (mostly AI/tech
  winners), so any outperformance is confounded with Roy's stock-picking. "You just
  picked winners" is the first critique a sharp reader makes.
- **Survivorship / look-ahead.** These are *today's* known names; backtesting on them
  over a past window is contaminated (you wouldn't have picked all 15 in 2021).
- **Doesn't scale or evolve.** 15 hand-picked names is a watchlist, not a universe.

---

## The target architecture: a funnel, point-in-time

```
All US equities
   │  ① Eligibility filter (hard rules, point-in-time)
   ▼
Investable universe  (hundreds)
   │  ② Cheap quant screen (candidate generation)
   ▼
Candidate set  (tens)
   │  ③ Multi-agent deep analysis  ← the bot, the expensive step
   ▼
Decisions (BUY/HOLD/SELL + confidence)
   │  ④ Portfolio construction (sizing, caps, risk limits)  ← partly built
   ▼
Orders
```

The guiding principle is the same rules-for-breadth / judgment-for-depth split the
rest of the system follows: **quant goes wide and cheap (eligibility + screen); the
LLM goes narrow and deep.** The LLM is *not* a screener — screening is not its
comparative advantage and it is far too expensive to use that way.

---

## Decision: eligibility = point-in-time S&P 500 membership

Chosen over a full-universe liquidity screen and over keeping the curated list,
because index membership is well-defined, liquid (no capacity concern at retail
size), self-maintaining, aligns with the existing SPY benchmark, and removes
curation bias (≈500 names nobody picked).

### Live vs backtest — the reframe that matters
Survivorship bias only affects **backtesting**, not live trading:
- **Live / real money** needs only the **current** S&P 500 membership — you trade
  today's constituents going forward. Cheaply available (Wikipedia / free sources).
  *For the real-money question, the universe problem is essentially solved and free.*
- **Honest backtesting** needs **historical** membership (what the index was on each
  past date). That is the harder/paid data.

### Data source
FMP's `sp500-constituent` and `historical-sp500-constituent` endpoints return **402
(restricted)** on the current tier — verified 2026-05-29. So:
- Backtest PIT membership comes from a **free historical dataset** — the commonly used
  `fja05680/sp500` GitHub CSV of periodic membership snapshots (`date, tickers`).
  Reconstruct as-of membership = latest snapshot with `snapshot_date <= decision_date`.
  (Verify exact filename/format at implementation time.)
- Live membership comes from the current free list.

### Integration (fits the existing loader pattern)
- New `src/data/universe.py` with `sp500_as_of(date) -> list[str]`, backed by the CSV
  downloaded once into `data/reference/` (cached/committed).
- `BacktestConfig.universe` (static `list[str]`) becomes a `universe_loader(date) ->
  list[str]` — same shape as the price/fundamentals loaders. Live mode calls
  `sp500_as_of(today)`.

### Data-quality caveats to plan around
- **Symbol renames** — the historical list uses the as-of ticker (FB, not META;
  GOOG/GOOGL classes). FMP lookups need the symbol FMP recognizes → a rename
  reconciliation step. Recent windows are mostly clean; deeper history is not.
- **Delisted/acquired names** — former constituents may have no FMP data on the free
  tier → they fail to load. Minor for a recent 6-month window, larger going back.
- **Validate the CSV** against a known reference date before trusting it.

---

## Consequences of choosing S&P 500

1. **The candidate screen (stage ②) becomes non-optional.** 500 names × 2 specialists
   × 26 weeks ≈ 26k LLM calls/backtest (≈13k for technicals alone even with the
   fundamentals signal cache). Infeasible — the screen must cut 500 → ~20–50.
2. **Data throughput.** ~500 tickers of price history exceeds FMP's 250 req/day free
   cap in one run → a one-time multi-day backfill, a paid tier, or a smaller base.
   **S&P 100** is a pragmatic middle that stays rule-based, point-in-time, and
   unbiased while keeping data/cost tractable.
3. **Universe becomes a loader**, not a static list (structural change above).

---

## The decision-universe subtlety (must not miss)

Each period, the set of names the bot must decide on is:

> **decision universe = current holdings ∪ fresh candidates**

If the bot only analyzes the fresh screen output, it can *buy* names but can never get
a *sell* on something it holds that dropped off the screen (or out of the index) —
exits silently break and positions get stranded. Held names must be re-evaluated every
period regardless of whether they still screen in. `decide_all` already takes a
`universe` list, so this is a "what do you pass in" requirement, not a rewrite.

---

## Decided: the candidate screen (stage ②) = event-driven selection

The screen is a **trigger, not a ranker**. A name becomes a candidate when *something
happens to it* — primarily a **fresh quarterly filing** — not because of what kind of
stock it is. This was chosen deliberately over ranking screens (factor / momentum /
biggest-mover) because every ranker tilts toward some style; the requirement was *no
style lean across the 500*.

Why event-driven satisfies the constraints:
- **No lean across the 500.** Every company reports on its own cycle, so a filing
  trigger *rotates attention across the entire universe* over the reporting cycle. It's
  a coverage mechanism, not a merit selector — favors no style.
- **No double-counting the specialists.** The trigger is "new information arrived," not
  "P/E is low" / "momentum is high." The specialists still do all the judging; the
  screen only decides *when to look*.
- **Direction-neutral.** A fresh filing implies nothing about up/down — agents decide.
- **Sentiment-native.** A filing/earnings event *is* a catalyst; the future sentiment
  specialist keys off the same event stream. Screen and specialist converge.
- **Self-pacing.** ~500 names reporting across a quarter ≈ ~38/week → the candidate set
  is naturally ~30–40/week with no arbitrary cutoff.

Composes with: **decision universe = (held names, always re-judged) ∪ (names with a
fresh event this period).** A holding can be sold even in a week it has no catalyst.

### Committed: quarterly-filings upgrade (prerequisite)
The data layer currently fetches **annual** statements (the "up to 15 months stale"
limitation). Annual triggers fire ~once/year per name → too sparse. Move fundamentals
to **quarterly** filings (FMP `period=quarter`): 4× the event cadence *and* it
independently fixes the staleness limitation. Implications to handle:
- YoY revenue growth must compare the same quarter a year prior (not sequential
  quarters) — `calc_growth_yoy` and the filing-pairing logic change.
- The fundamentals prompt's "annual filings, up to 15 months stale" framing updates.
- Eval re-calibration: `ConsistencyEval` / grounding evals run against the new
  quarterly-sourced values.

### Deferred: price-move trigger → folded into the sentiment specialist
A "notable price move" trigger would make the bot responsive to mid-cycle events, but
it carries a mild volatility tilt (brushes the no-lean preference) and "the market
moved on news" is really a *sentiment* signal. So it belongs in the **sentiment
specialist** (next major build, near-term) rather than bolted onto the screen now.

---

## Relationship to releases

- **Release 1** ships on the curated watchlist, explicitly
  labeled a *watchlist*, with survivorship disclosed as a known limitation.
- **Release 2** introduces this funnel: point-in-time S&P 500 (free CSV) → screen →
  analysis. Supersedes the "single-factor baselines use the same universe" and
  "survivorship" limitations in `notes/backtest-design.md`.
