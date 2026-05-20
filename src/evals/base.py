"""
src/evals/base.py holds the contract. Three things:

EvalResult — a Pydantic model. passed: bool, score: float | None, details: dict[str, Any], plus metadata like eval_name: str and ticker: str | None. The details dict is the escape hatch for eval-specific structured output (the judge's explanation, which claims failed, etc.).
Eval — a Protocol or ABC with an async run(input) -> EvalResult method. Use Protocol; it's lighter and fits how you've structured specialists.
EvalRunner — a simple class or function that takes a list of Eval instances and a list of inputs, runs them (probably with asyncio.gather), and returns a list of EvalResult. Can be 20 lines. Don't overbuild.

"""
import asyncio
from typing import Any, Dict, List, Optional, Protocol
from pydantic import BaseModel, Field

# ==========================================
# 1. EVALUATION DATA MODEL
# ==========================================

class EvalResult(BaseModel):
    """
    The standardized output format for all evaluations.
    The details dict is the escape hatch for eval-specific structured output 
    (e.g., the judge's explanation, which claims failed, etc.).
    """
    eval_name: str
    passed: bool
    score: Optional[float] = None
    ticker: Optional[str] = None
    
    # Using Field(default_factory=dict) ensures we always have an empty dict 
    # instead of None if an evaluator forgets to pass details.
    details: Dict[str, Any] = Field(default_factory=dict)

# ==========================================
# 2. EVALUATOR CONTRACT
# ==========================================

class EvalProtocol(Protocol):
    """
    Structural contract for any evaluator.
    Whether it's an LLM Judge or a deterministic schema checker, 
    it just needs to implement this method.
    """
    async def run(self, input_data: Any) -> EvalResult:
        ...

# ==========================================
# 3. CONCURRENT RUNNER
# ==========================================

class EvalRunner:
    """Orchestrator for running multiple evals against multiple inputs."""
    
    @staticmethod
    async def run_evals(evals: List[EvalProtocol], inputs: List[Any]) -> List[EvalResult]:
        """
        Executes every evaluator against every input concurrently.
        Returns a flattened list of all EvalResult objects.
        """
        tasks = []
        
        # Build the Cartesian product of evals and inputs
        for eval_instance in evals:
            for input_data in inputs:
                tasks.append(eval_instance.run(input_data))
                
        # Fire them all off concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out exceptions if an eval crashed, or handle them gracefully
        clean_results = []
        for r in results:
            if isinstance(r, Exception):
                # You could log the exception here in a real production setup
                print(f"Eval failed with error: {r}")
            else:
                clean_results.append(r)
                
        return clean_results