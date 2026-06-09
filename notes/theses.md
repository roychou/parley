# Theses

Running observations from the build. Not structured writing — working material for design notes and blog posts. Entries should be specific and opinionated, not hedged.

---

## What the first eval runs surfaced about specialist reasoning quality

The fundamentals and technicals grounding evals ran across MSFT, TSLA, and NVDA in Week 2. The headline result — judge passes, no grounding failures — was less interesting than what the outputs revealed about *how* the specialists reason.

The most useful observation: LLM financial analysts fail like biased humans, not like miscalibrated calculators. The failure mode isn't hallucinated numbers — the specialists generally cited the right values. The failure mode is selective framing of ambiguous cases. On MSFT, the specialist treated 14.93% revenue growth as "effectively at" the 15% strong-growth threshold and used it as bullish evidence. The number was correct. The characterization was directionally loaded. A human analyst with a bullish thesis would do the same thing.

This matters for eval design in a specific way. Grounding evals as designed — "does the reasoning cite the data correctly?" — catch numeric hallucinations cleanly. They catch directional mischaracterizations cleanly when the gap is large (calling 35% margins "weak"). They catch almost nothing in the ambiguous middle where a characterization is technically defensible but consistently skews in one direction. That middle band is where real analyst bias lives, and it's currently untested.

The planted-failure calibration pattern also surfaced something fundamental about LLM-as-judge evaluation: all-pass results prove nothing in isolation. Three passing eval runs are consistent with both "the judge works correctly" and "the judge rubber-stamps everything." The planted contradiction is what distinguishes them — and the fact that you have to construct it deliberately means you have to have a prior on what the failure mode is before you can test for it. For the grounding eval, that prior was obvious (numeric hallucination, directional mischaracterization). For future eval types covering subtler failures, finding the right planted contradiction will require more domain work before the eval can be trusted.

The broader implication: eval harnesses for financial agents are only as good as the failure taxonomy the designer brings to them. The grounding eval is well-calibrated for the failures it was designed to catch. It is not a general-purpose quality gate. Shipping it under the label "eval harness" without that qualification would be misleading — the honest framing is "grounding eval, calibrated against hallucination and directional mischaracterization, untested on subtle characterization bias."
