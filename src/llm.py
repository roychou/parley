"""Shared LLM-boundary types.

`MessageCreator` is the slim contract the specialists depend on — just the
`.create(**params) -> Message` slice of the Messages API. Both the live path
(`client.messages`) and the batch-backed path (`backtest.batch.BatchLLM`) satisfy
it, so either can be injected at the call sites without the agents layer having to
know which one it got (and without agents importing from backtest)."""
from __future__ import annotations

from typing import Any, Protocol

from anthropic.types import Message


class MessageCreator(Protocol):
    async def create(self, **params: Any) -> Message: ...
