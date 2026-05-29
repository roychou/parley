"""
Event-driven candidate selection — the multi-agent strategy's attention mechanism.

A name is a candidate when it *filed* (10-Q/10-K) within the period since the
last decision — a trigger, not a ranker, so it imposes no style lean and rotates
attention across the whole index over the reporting cycle. The decision universe
is candidates UNION current holdings, so a held name can always be re-judged (and
sold) even in a period it filed nothing or after it left the index.

See notes/universe-design.md ("Decided: the candidate screen = event-driven").
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

FilingDatesFn = Callable[[str], list[str]]


def select_candidates(
    eligible: Iterable[str],
    held: Iterable[str],
    window_start: str,
    window_end: str,
    filing_dates_fn: FilingDatesFn,
) -> list[str]:
    """Tickers to analyze this period.

    = eligible names that filed in (window_start, window_end]  (start exclusive,
      end inclusive; YYYY-MM-DD strings)
    UNION all held names (regardless of eligibility — exits must stay possible).
    """
    candidates = set(held)
    for ticker in eligible:
        if any(window_start < fd <= window_end for fd in filing_dates_fn(ticker)):
            candidates.add(ticker)
    return sorted(candidates)
