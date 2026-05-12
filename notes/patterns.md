## 4 May 2026
- yfinance.Ticker(symbol).history(period="1mo") returns DataFrame
- DataFrame iteration: for date, row in df.iterrows()
- pathlib pattern: Path(...).mkdir(parents=True, exist_ok=True)
- yfinance period="1mo" is calendar-month-back, not a fixed start date
- Market holidays produce gaps; don't assume continuous business days
- For backtests later: pandas-market-calendars for proper trading-day awareness
- Always verify model strings — Anthropic ships fast and memory goes stale
- Default Sonnet ID as of today: claude-sonnet-4-6
- Budget heuristic: Sonnet for everything until burn rate forces a split
- Tool use loop pattern: check stop_reason == "tool_use", append assistant content, build tool_result blocks, send back, loop until end_turn
- response.content is a list of typed blocks — filter by .type (tool_use, text, etc.)
- Pydantic schema → tool input_schema via model_json_schema()
- Validate tool inputs by reconstructing the Pydantic model from tool_use.input
- Long string literals: wrap with parens and adjacent literals; Python concatenates at parse time
## 5 May 2026
- pandas Series rolling operations — series.rolling(window=N).mean() for SMA, series.ewm(alpha=1/N, min_periods=N, adjust=False).mean() for Wilder smoothing.
- NaN handling on rolling windows — series.dropna().iloc[-1] is the idiom for "latest non-NaN value." min_periods=window in ewm enforces NaN until full window.
- pytest float assertions — pytest.approx(value) not ==. Series-vs-scalar comparison ambiguity ("truth value is ambiguous" error).
- Pydantic to JSON schema for tools — MyModel.model_json_schema() is the bridge.
- Anthropic SDK tool-use loop shape — while response.stop_reason == "tool_use", append assistant content, build tool_result blocks with matching tool_use_id, append as user role, re-call.
- Parallel tool calls — one assistant turn can contain multiple tool_use blocks; iterate over [b for b in response.content if b.type == "tool_use"].
- JSON serialization gotcha — numpy floats from yfinance choke json.dumps; cast with float(...) at the boundary.
- Empty DataFrame handling in yfinance — df.empty check. Wrap loops in try/except because failure modes are diverse (HTTP, parse, ValueError).
- Cache contract — schema lives in the JSON file format, not in the function signature. Lowercase vs capitalized keys was today's gotcha.
- Tool descriptions as prompts — vague description produces default-y model behavior. The 1mo default got picked because nothing in the description said when to choose otherwise.
- Silent contract mismatch — Pydantic-validated args that the function ignores. The period argument in get_price_history.
- Hallucination on missing date anchors — without as_of envelope on tool results and current-date in system prompt, the model invents date ranges that don't exist.
## 7 May 2026
- Pydantic field descriptions on output schemas function as model instructions during structured output generation. When two fields need to agree (e.g., `as_of` matching the tool result's date), the cross-reference belongs in the description text, not just inferred from field names.
- System prompts for agents read better and steer better when split into sections (role, workflow, indicator rules, constraints) rather than written as continuous prose. Run-on prompts blur structure the model would otherwise latch onto.
- When a specialist combines multiple signals into one output, specify the conflict-resolution rule explicitly (e.g., "weight trend over momentum when conflicting"). Without it the model picks arbitrarily and the same inputs can produce different outputs across runs.
- FastMCP only populates `result.structuredContent` when the tool's return type is annotated with a Pydantic model (or other schema-bearing type). Without that, structured data lives in `result.content[0].text` as a JSON string and must be parsed. Fix later by typing the server tool's return.
- MCP server scripts launched directly by `mcp dev` or `python` don't inherit the project root on `sys.path`. Either install the package in editable mode (`uv pip install -e .`) or add a sys.path shim at the top of each entry-point script. Pick one approach project-wide later.
- Tool input schemas in the Anthropic SDK are JSON Schema dicts. Pydantic's `.model_json_schema()` generates these automatically; for tools with one or two trivial inputs, hand-writing the dict is shorter. Reserve Pydantic for tools whose inputs warrant validation or shared use elsewhere.
- `submit_analysis` is the model's exit signal for structured-output specialists, not a dispatch target. Handle it inline in the agent loop with an early return; only true data-fetching tools route through `dispatch_tool`.
- `GetTechnicalsInput` is duplicated across `single_agent.py` and `technicals_specialist.py`. Consolidate into shared schemas module once the agents directory stabilizes (post-supervisor). Don't refactor mid-build.
- Eval gap surfaced: the technicals tool exposes SMA-20 and RSI-14 values but no current price, so the prompt rule "price above SMA-20 = bullish" can't be cleanly evaluated. The model correctly hedged ("consistent with"). Either add current price to the tool result or rewrite the trend rule in indicator-only terms (e.g., compare SMA-20 to SMA-50). Day 16+ decision.
## 8 May 2026
## Day 17 — fundamentals specialist
### Fundamentals data has two temporal regimes
Filings (profit_margin, revenue_growth_yoy, debt_to_equity, diluted_eps) update quarterly when the company files a 10-Q or 10-K — static between filings. Price (the numerator of P/E) is daily. Cache and agent prompt both need to reflect this. Practical implication: cache keyed by fetch date for management, but `as_of` inside the JSON is the filing date. P/E is computed at read time by combining cached filings with cached prices, not stored in the filings cache.

### `Ticker.financials` columns are fiscal period-ends, not filing dates
Verified empirically (MSFT shows June year-ends, AAPL would show September). Period-end ≠ filing date — companies file 60-90 days after period close. For an honest trading bot, the data is anchored to period-end with the understanding that the trader couldn't have known the data until ~3 months later. For v1 this is acceptable; for strict point-in-time backtests it's a known limitation that requires filing-date data yfinance doesn't expose.

### Schema design is not a copy-paste exercise across specialists
Started by reflexively adding `date_range` to `FundamentalsAnalysis` because `TechnicalAnalysis` had it. Stopped to ask what `date_range` would even mean for a point-in-time fundamentals snapshot. Answer: nothing useful. Dropped it. Schemas should reflect the actual temporal shape of the data, not be retrofitted from another specialist's solution to a different problem.

### Threshold rules > sector-comparison rules when the agent doesn't have peer data
Initial fundamentals prompt told the agent to "compare against sector average." But the tool only returns metrics for one ticker, with no sector benchmark. The agent would either hallucinate sector ranges or hedge to NEUTRAL. Replaced with absolute thresholds (P/E > 40 high, profit margin > 20% strong, etc.). Cruder but eval-able and grounded. Adding sector context is a Release 2 improvement if eval results show it matters.

### Agent-judgment behavior worth monitoring: rounding near thresholds toward favorable
On MSFT, the agent treated 14.93% revenue growth as "effectively at" the 15% strong threshold and used it as bullish-supporting in the synthesis. Reasonable analyst behavior, but worth watching across more tickers. Risk: agent rounds toward the bull case more often than the bear case, biasing signals.

### yfinance `Ticker.info["sharesOutstanding"]` not needed if `Diluted EPS` is in `financials`
Initially planned to compute EPS from Net Income / Shares Outstanding. `Ticker.financials` has `Diluted EPS` as a row directly, which removes the dependency on `.info` (which has its own data quality issues). Cleaner: pull EPS from financials, multiply by current price for P/E.

### Multiple yfinance calls per ticker is a smell
Current `fundamentals.py` calls `yf.Ticker(ticker)` 4+ times to compute the metric set. Each calculator function makes its own round trip. Should be one fetch of `financials` and one fetch of `balance_sheet`, then pure functions over those DataFrames. Refactor scheduled for Day 20.

### Fetch-with-cache pattern is the right caller interface
Callers should hit a single `fetch(ticker, force_refresh=False)` that returns from cache if fresh, otherwise hits yfinance and writes the cache. Filings staleness threshold of "same-day cache is fresh" is appropriate — quarterly filings don't change within a day. Distinct from price cache freshness semantics. Refactor scheduled for Day 20.