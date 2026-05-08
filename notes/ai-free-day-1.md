# AI-free rule, Day 1 retro

The rule held in places and was suspended in others. Both deliberate, both worth naming honestly.

Held:
- Pydantic schema design (`TechnicalAnalysis`) — wrote it AI-off, got the structure right, missed the `Literal` import and a docstring carryover. Conceptual gaps were small; mechanical fixes after.
- System prompt for the technicals specialist — wrote AI-off, came back as run-on prose, rewrote AI-off into a structured version after critique. Real muscle exercise.
- The `_helpers.py` `build_system_prompt` function — five lines, AI-off, syntax errors only (wrong import form, missing `%` in format string). Shape was right first try.

Suspended (AI-on):
- MCP server scaffold — named exception in the original plan; new protocol territory.
- MCP client wiring (`stdio_client`, `ClientSession`) — also new protocol territory; this was the right call, the async API is non-obvious from the spec alone.
- Agent loop body — this was the real concession. The plan said AI-off for dispatch logic; in practice I wrote a draft, hit several conceptual issues (sync function with await, wrong exit condition, dispatch trying to handle the exit signal), and worked through them with AI feedback rather than re-deriving from `single_agent.py` alone.

Honest read on the concession: the agent-loop conceptual gaps (async vs sync, exit-on-submit pattern, dispatch separation) are exactly the muscles the rule was supposed to build. Letting AI walk me through them means I haven't built them today. The fundamentals specialist on Day 16 should test whether they actually landed: same shape, copy-and-modify pattern, AI-off this time including the loop body. If I hit the same conceptual gaps tomorrow, the rule didn't work; if I don't, today's AI-assisted debugging counts as scaffolding rather than substitution.

Friction points worth naming:
- "I don't remember the exact Pydantic syntax" was the most common AI-on temptation. Reading docs filled the gap fine when I forced it; the temptation surfaced because docs feel slower than asking.
- MCP plumbing genuinely required external help — spec alone wasn't enough to know `result.structuredContent` would be `None` without an output schema annotation. That's a real gap in the spec docs, not a discipline failure.
- The agent loop was where I conflated "I've written this before" (Day 14's two-tool single agent) with "I can write this from scratch." The structural similarity made me overconfident; the structural differences (async, structured output via tool, early return) are non-trivial.

Carryover for Day 16:
- Fundamentals specialist: copy `technicals_specialist.py`'s shape, adapt for fundamentals indicators. Loop body should be AI-off this time.
- Pytest smoke test for the technicals specialist (slipped from today; wasn't started).
- Multi-ticker verification (only ran on TSLA; smoke test should cover 3+).
- Fix FastMCP output-schema annotation to populate `structuredContent` properly.