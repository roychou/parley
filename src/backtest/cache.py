"""
Disk-backed per-specialist signal cache.

Each specialist's analysis is cached independently, keyed by
(kind, ticker, data_version) and namespaced by a per-kind prompt version.

Why signal-level rather than Decision-level:
- Selective invalidation. Bumping one specialist's prompt version only
  invalidates that specialist; the others stay warm.
- No stale synthesis. Synthesis is recomputed from the cached signals every
  time (it is cheap and deterministic), so changing synthesize() never serves
  a stale Decision.
- Per-specialist cadence. `data_version` identifies the inputs a signal was
  computed from, so a specialist whose inputs change slowly is reused across
  many decision dates. The fundamentals signal keys on (filing_date, pe_band):
  it recomputes only when a new filing lands or P/E crosses a threshold band,
  not every day. Technicals key on as_of (they genuinely change daily).
- Extensible. A new specialist (e.g. sentiment) plugs in as another `kind`
  with its own `data_version` + version, no change to the supervisor loop.

`make_cached_provider`-style Decision-level caching was removed in favour of
this: a Decision is now always re-synthesized from the (cached) signals.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


class SignalCache:
    """One JSON file per (kind, ticker, data_version) under <root>/<kind>/<version>/."""

    def __init__(
        self,
        root_dir: Path | str,
        versions: dict[str, str] | None = None,
        default_version: str = "v1",
    ):
        self.root = Path(root_dir)
        # Per-kind prompt versions; kinds absent here fall back to default_version.
        self.versions = dict(versions or {})
        self.default_version = default_version

    def version_for(self, kind: str) -> str:
        return self.versions.get(kind, self.default_version)

    def get(self, kind: str, ticker: str, data_version: str, model_cls: type[M]) -> M | None:
        path = self._path(kind, ticker, data_version)
        if not path.exists():
            return None
        try:
            return model_cls.model_validate_json(path.read_text())
        except Exception as e:
            logger.warning(
                f"Signal cache read failed for {kind}/{ticker}@{data_version}: {e}. Cache miss."
            )
            return None

    def set(self, kind: str, ticker: str, data_version: str, signal: BaseModel) -> None:
        path = self._path(kind, ticker, data_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(signal.model_dump_json(indent=2))

    def _path(self, kind: str, ticker: str, data_version: str) -> Path:
        return self.root / kind / self.version_for(kind) / f"{ticker}_{data_version}.json"


async def cached_signal(
    cache: SignalCache | None,
    kind: str,
    ticker: str,
    data_version: str,
    model_cls: type[M],
    compute: Callable[[], Awaitable[M]],
) -> M:
    """Return a cached specialist signal, or compute + store it on a miss.

    `compute` is a zero-arg coroutine factory (e.g. a lambda wrapping the LLM
    call). With cache=None the signal is always computed (uncached path).
    """
    if cache is not None:
        hit = cache.get(kind, ticker, data_version, model_cls)
        if hit is not None:
            return hit
    result = await compute()
    if cache is not None:
        cache.set(kind, ticker, data_version, result)
    return result
