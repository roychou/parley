# IBKR execution adapter (Level B)

`src/forward/ibkr_execution.py` places orders against the IBKR **paper** account and
reads real positions/equity back — so the forward clock can run against the broker
instead of the simulated `PaperBook`. The truest dress rehearsal short of live money.

## Safety model (this is the only code that transmits orders)

1. **Paper-account guard.** `_assert_paper` requires every managed account id to start
   `DU` (IBKR paper). It refuses to act against a live account and is re-checked
   immediately before any order is sent. (Our account: `DUQ576452`.)
2. **Preview by default.** `transmit=False` everywhere — orders are logged, never sent.
   Real placement needs `transmit=True` **and** IB Gateway's **Read-Only API turned
   OFF** (it's ON now, which is why reads/previews work but placement is blocked).
3. **Bounded sizing.** Market orders only; OPENs = `floor(equity × target_weight ÷
   price)` against the *real* account equity, CLOSEs sell exactly the shares held.
   Sub-share and over-equity orders are dropped.

## The path

`broker_rebalance(ib, decisions, prices, vols=, risk_config=, equity_curve=, transmit=)`:
1. `account_state(ib)` — real equity, cash, positions (+ avg cost).
2. reconstruct a Portfolio mirroring the account → `build_actions` (same risk layer as
   the backtest: inverse-vol sizing, per-name/sector/gross caps, drawdown governor).
3. `plan_orders` → whole-share BUY/SELL market-order plans.
4. `execute_orders(transmit=…)` → preview (log) or place + await fills.

## Validated

Live against the paper account (Gateway 4002): account read ($1,000,000, 0 positions),
and the full `broker_rebalance` in **preview** sized 3 decisions correctly
(ADSK BUY 250, WMT BUY 526 at 6% each; an unheld SELL skipped) — **nothing transmitted**.
Pure planning + the paper guard are unit-tested.

## Going live (deliberate, opt-in)

1. In IB Gateway: Configure → Settings → API → **uncheck "Read-Only API"**, apply.
2. Run with preview first and **review the planned orders**.
3. Then `transmit=True`. Note the paper account starts at **$1M** (not the sim book's
   $100k), so sizing is against $1M.

## Remaining wiring (next step)

`run.py` doesn't yet expose `--execute ibkr`; the weekly run still uses the simulated
PaperBook. To make the scheduled clock transmit: wire `broker_rebalance` into
`run_forward_paper_session` (feed it the session's decisions/vols/prices and the
PaperBook's equity_curve for the drawdown governor), then reconcile the book from the
returned fills. Keep `--execute sim` (the simulated PaperBook) as the default; gate
transmit behind an explicit flag.
