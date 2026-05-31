"""
LLM spend cap — a hard safety rail on a run's API cost.

A full-index × multi-date run can balloon (the clean-window validation hit ~$120
across two credit-exhausting runs — far past the estimate). This wraps the injected
`MessageCreator` so every `.create()` — fundamentals, technicals, the sentiment
map/reduce/synthesis, news — accrues an estimated cost; when the running total
crosses `max_usd`, the next call raises `BudgetExceededError`, which the strategy loop
treats as fatal and aborts the run. Because every computed signal is cached on disk
first, an abort loses no work: raise the cap and re-run to resume from the warm cache.

The estimate is tokens × list price (× batch discount). It's a *safety rail*, not a
precise meter — with batching the cap is enforced at wave granularity, so it can
overshoot by up to one in-flight wave. Pricing is conservative when unsure (caps
earlier). Update PRICE if the specialist models change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from anthropic.types import Message

from src.llm import MessageCreator

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output), list price. Source: Claude models overview.
PRICE: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class BudgetExceededError(RuntimeError):
    """Raised when a run's estimated LLM spend crosses the configured cap. The word
    'budget' is in the message so decide_all treats it as a fatal (abort) error."""


@dataclass
class BudgetMeter:
    """Accumulates estimated LLM spend and trips at max_usd."""
    max_usd: float | None
    batch_discount: float = 1.0          # 0.5 when routed through the Batch API
    spent: float = 0.0
    calls: int = 0

    def charge(self, model: str, input_tokens: int, output_tokens: int) -> None:
        rate = PRICE.get(model)
        if rate is not None:
            self.spent += (input_tokens / 1e6 * rate[0] + output_tokens / 1e6 * rate[1]) \
                * self.batch_discount
        self.calls += 1
        if self.max_usd is not None and self.spent > self.max_usd:
            raise BudgetExceededError(
                f"LLM budget cap ${self.max_usd:.2f} exceeded (estimated ~${self.spent:.2f} "
                f"over {self.calls} calls) — run aborted. Computed signals are cached; raise "
                f"--max-llm-usd and re-run to resume from the warm cache."
            )


@dataclass
class BudgetedMessages:
    """A MessageCreator that meters spend through a BudgetMeter, then delegates."""
    inner: MessageCreator
    meter: BudgetMeter = field(default_factory=lambda: BudgetMeter(None))

    async def create(self, **params: Any) -> Message:
        resp = await self.inner.create(**params)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.meter.charge(
                params.get("model", ""),
                getattr(usage, "input_tokens", 0),
                getattr(usage, "output_tokens", 0),
            )
        return resp
