## Options for supervisor design

1. A list of analyses, schema-agnostic, and the supervisor reasons in prose
2. A typed SpecialistOutputs container with named fields per specialist
3. A normalized Signal interface that all specialists implement, so synthesis sees a uniform shape regardless of which specialists ran

- Chose Option 3: it seems like the most extensible (news, risk, anything else slots in cleanly) but commits me to a contract specialists have to conform to.
- The idea. Every specialist, regardless of what it analyzes, produces output that conforms to a shared base contract. The supervisor and synthesis logic operate on that contract, not on specialist-specific types. Adding a new specialist means implementing the contract — no changes to supervisor or synthesis code.

<!-- # src/schemas/signal.py
from pydantic import BaseModel, Field
from typing import Literal

SignalDirection = Literal["BULLISH", "BEARISH", "NEUTRAL"]

class SpecialistSignal(BaseModel):
    """Base contract every specialist output conforms to."""
    specialist: str              # "fundamentals", "technicals", "news", "risk"
    signal: SignalDirection
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=50)
    as_of: str                   # YYYY-MM-DD
    evidence: dict[str, float | str | None]  # specialist-specific supporting data -->

- Option A: specialists return SpecialistSignal directly. The specialist-specific data (P/E, profit margin, SMA, RSI) goes into the evidence dict. Simple, uniform, but loses Pydantic typing on the evidence — evidence["pe_ratio"] is float | str | None, not a typed field.
- Option B: specialists return a subclass. FundamentalsAnalysis(SpecialistSignal) adds typed fields like pe_ratio: float | None, profit_margin: float | None. Synthesis still operates on the SpecialistSignal interface (signal, confidence, reasoning), ignoring subclass fields. Eval harness can downcast when it needs specialist-specific data.
- Option B is the better answer for my use case. You keep typed access to specialist-specific data where it matters (evals, debugging, blog posts about per-specialist behavior), and synthesis stays generic.
<!-- # src/supervisor.py
async def run_supervisor(ticker: str, question: str) -> Decision:
    signals: list[SpecialistSignal] = await asyncio.gather(
        run_fundamentals_specialist(ticker),
        run_technicals_specialist(ticker),
    )
    return synthesize(ticker, signals) -->

- The supervisor doesn't know or care that one signal is a FundamentalsAnalysis and the other is a TechnicalsAnalysis. It collects a list[SpecialistSignal] and hands them to synthesis.
- What synthesis sees.
<!-- # src/synthesis.py
def synthesize(ticker: str, signals: list[SpecialistSignal]) -> Decision:
    # Stub: simple confidence-weighted vote
    score = sum(
        s.confidence * {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}[s.signal]
        for s in signals
    )
    direction = "BUY" if score > 0.3 else "SELL" if score < -0.3 else "HOLD"
    return Decision(
        ticker=ticker,
        direction=direction,
        score=score,
        contributing_signals=signals,
        rationale=f"{len(signals)} specialists: {[s.specialist for s in signals]}",
    ) -->

- Synthesis operates entirely on the base contract. When Release 2 adds news and risk specialists, this code does not change. That's the extensibility win.
