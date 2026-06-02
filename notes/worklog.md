# Worklog

Reverse-chronological daily close-outs. The durable roadmap/status lives in
[productization.md](productization.md); this is the running log of what happened.

---

## 2026-06-01 — validation pass, data-layer hardening, universe switch

**Theme:** Anthropic credits topped up → ran the first real-LLM validation, which
turned into a productive bug hunt, then switched the universe to Nasdaq-100.

**Blocker (unchanged):** IBKR paper account still pending approval. It gates the
forward clock — the only thing that yields a real edge verdict. Everything upstream
is now built/fixed/validated.

**Shipped (committed):**
- Project-wide lint/bug cleanup (3 F821 bugs + 281 lint → 0).
- **SPY-benchmark commission fix** — a 100%-of-cash order was rejected when commission
  tipped it over cash, so the benchmark never traded → vs-SPY/alpha-beta silently null
  in every realistic-cost run. Now shrinks-to-fit.
- **EDGAR fixes + recency guard** — revenue-concept selection was serving years-stale
  filings (NVDA→2019, XOM→2023); YoY matched by exact MM-DD (broke 52/53-week
  calendars → AAPL rev_growth NaN). Fixed both; added a recency guard that abstains on
  stale data rather than trading on it.
- **MCP servers load `.env`** + `EDGAR_USER_AGENT` set — the live/forward specialist
  path was fully broken (sanitized subprocess env) and is now working.
- **Grounding-judge + specialist-prompt fixes** — judge no longer false-flags fiscal
  dates; prompts de-rotted (stale field refs removed, 0.0≠missing, quarterly not
  annual, no temporal overreach). Live probe went to 3/3 fundamentals + 3/3 technicals.
- **Universe: S&P 500 → point-in-time Nasdaq-100** from QQQ SEC N-PORT filings (CIK
  1067839), name+CUSIP→ticker resolution, `--nasdaq100` flag. Authoritative, free,
  point-in-time back to 2019.
- **FMP fundamentals fallback** for foreign filers EDGAR can't serve (ASML, PDD, etc.)
  — fixes a silent forward-universe gap too. Needs FMP Premium (stable API).

**Validation findings (honest):**
- Eval suite 9/9 (~$0.06); it tests the *judge*, not live specialists.
- Live specialists are well-grounded on correct data; the fundamentals 0/3 we first
  saw was entirely the stale-data bug.
- Backtests (clean window, run #4 15-name and run #5 14-name Nasdaq-100∖S&P-500):
  plumbing fully validated, **no edge demonstrated**. Run #5's +9.35% / "+29% alpha
  vs SPY" is a benchmark illusion — the high-beta basket rallied ~9-10% while SPY was
  flat, so vs-SPY = universe beta. On the same names, multi_agent ≈ random, < pe_ranking.
  Only differentiator: lowest drawdown / fewest trades (noise-level at 16 dates).
  Behavioral flag for forward: 57 SELLs / 33% hit in a rising tape = cuts winners early.
- Total session LLM spend ≈ $6 (evals + probes + two backtests), all under caps.

**Next (gated on IBKR):** open paper account → Gateway + data sub → live-validate
adapters → start the weekly forward clock with the news on/off ablation. No upstream
build moves the needle until then.
