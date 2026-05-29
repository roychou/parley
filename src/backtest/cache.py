"""
Disk-backed cache for supervisor decisions, keyed by (ticker, date) and
namespaced by a `version` string.

The version represents "the specialist-prompt + schema version." If the
fundamentals or technicals prompts change, bump the version string — old
cache entries under the prior version remain on disk but are silently
ignored by lookups under the new version. This is intentional: stale
results from an older prompt should not silently feed into new backtest
runs.

`make_cached_provider(supervisor_fn, cache)` returns a DecisionProvider
matching the injection point in MultiAgentStrategy. Tests use stubs;
production wires this with the real supervisor.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from src.schemas import Decision

logger = logging.getLogger(__name__)


class DecisionCache:
    """Disk-backed JSON cache. One file per (ticker, date) under <root>/<version>/."""

    def __init__(self, root_dir: Path | str, version: str = "v1"):
        self.root = Path(root_dir)
        self.version = version
        self._dir = self.root / self.version
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, ticker: str, date: str) -> Decision | None:
        path = self._path(ticker, date)
        if not path.exists():
            return None
        try:
            return Decision.model_validate_json(path.read_text())
        except Exception as e:
            logger.warning(f"Cache read failed for {ticker}@{date}: {e}. Treating as miss.")
            return None

    def set(self, ticker: str, date: str, decision: Decision) -> None:
        path = self._path(ticker, date)
        path.write_text(decision.model_dump_json(indent=2))

    def _path(self, ticker: str, date: str) -> Path:
        return self._dir / f"{ticker}_{date}.json"


SupervisorFn = Callable[[str, str], Awaitable[Decision]]
DecisionProvider = Callable[[str, str], Awaitable[Decision]]


def make_cached_provider(supervisor_fn: SupervisorFn, cache: DecisionCache) -> DecisionProvider:
    """Wrap a supervisor call with a (ticker, date) cache.

    Returns a function that conforms to MultiAgentStrategy's DecisionProvider
    type. Cache hits return immediately; misses call the supervisor and store.
    """
    async def provider(ticker: str, date: str) -> Decision:
        cached = cache.get(ticker, date)
        if cached is not None:
            return cached
        decision = await supervisor_fn(ticker, date)
        cache.set(ticker, date, decision)
        return decision
    return provider
