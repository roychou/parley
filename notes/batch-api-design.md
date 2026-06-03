# Batch API adapter — beating tier-1 throttling at index scale

> Status: prototype built + validated end-to-end (MSFT, real Batch API). Off by
> default; opt-in via `--batch`. Supersedes the per-filing semaphore as the
> scale answer (the semaphore stays as the live-path burst guard).

## The problem it solves

The backtest fans out hard. `MultiAgentStrategy.decide_all` gathers **every**
screened candidate for a decision date at once, and each candidate fans out again
(fundamentals + technicals, plus the sentiment scaffold's map → reduce → synthesis).
At S&P 500 scale a single decision date launches dozens of tickers × several calls
concurrently — past tier-1 input-tokens-per-minute on **both** Haiku and Sonnet
(it's aggregate, not per-model-trivial). The scaffold's `max_concurrent_chunks`
semaphore bounds *one* filing's burst; it can't bound the cross-ticker aggregate.

## The design

`BatchLLM` (src/backtest/batch.py) mimics `client.messages` — same
`.create(**params) -> Message` contract — so it drops into the existing injection
seam (`MessageCreator` protocol in src/llm.py) with **zero changes to specialist
logic**. Instead of firing each call inline it **debounce-coalesces** every call
that lands in one quiescent window into a single Message Batches job, routes
results back to each caller's future by `custom_id`, and raises per-request errors
only for the affected caller.

Injection path: `run.py --batch` → `build_strategies(use_batch=True)` builds one
`BatchLLM(client)` + a high-concurrency `ScaffoldConfig(max_concurrent_chunks=10_000)`
→ `partial(run_backtest_supervisor, messages_api=…, scaffold_config=…)` → threaded
to both numeric specialists and `run_sentiment_specialist` → the scaffold's `_make_llm`.
(High concurrency in batch mode so the map fan-out coalesces into one wave rather
than being throttled into many tiny batches by the live semaphore.)

Why batch fits: ~50% cheaper, its own far-larger throughput allowance (no per-minute
throttle), and a backtest is latency-insensitive — the textbook batch workload.

## The scaling insight (the whole payoff)

Dependency chains form **successive waves** automatically (a reduce call only fires
once its map futures resolve). Validated on MSFT: 5 sequential waves
(current-map ×7 → current-reduce ×1 → prior-map ×7 → prior-reduce ×1 → synthesis ×1),
~394s total because each batch wave carries ~60–90s of fixed latency.

Crucially that wave **depth is per decision date, not per ticker**. Because
`decide_all` gathers all candidates, every ticker's wave-1 calls coalesce into *one*
batch, every ticker's reduce calls into the next, and so on. So ~500 names cost
roughly the **same wall-clock as one** (just larger batches) — with zero throttling.
A full S&P 500 + sentiment run is therefore ≈ (waves per date) × (~60–90s) × (#dates),
on the order of tens of minutes, instead of crawling against the 50k/min wall.

Trade-off owned: higher per-ticker latency in exchange for unthrottled,
~half-price throughput at index scale. For interactive/live use, keep the inline
path (the semaphore handles a single filing fine).

## Not done / Release-2

- **Agentic retrieval** is the modern *quality* upgrade (orthogonal to batching):
  give the specialist a `search_filing(query)` tool so it reads only the passages it
  wants instead of map-reducing the whole narrative. Lighter than a full RLM scaffold
  (which is overkill at 10-K sizes — see sentiment-specialist-design.md), and it cuts
  tokens, helping cost *and* throughput. (Parked in productization.md → Phase 5+.)
- A global token-bucket limiter is the alternative if we ever need the *inline* path
  at scale (keeps tier 1, paces the whole run); batch is the better answer for backtests.
