#!/usr/bin/env bash
# Model-cutoff ladder run — Sonnet 4.5 (training cutoff Jul 2025), FULL TOOL w/ HYBRID sentiment.
#
# Sonnet 4.0 (Mar-2025 cutoff) is no longer accessible on this account (404), so Sonnet 4.5 is
# the best cheap+available rung. 39 clean weekly Fridays (Aug 2025 → Apr 2026), ~88-90 covered
# Nasdaq-100 names/date, over a window Sonnet 4.5 was NOT trained on.
# Specialists: fundamentals + technicals + HYBRID sentiment (carry-forward tone memo + diff).
# Synchronous; dates oldest-first so the carried tone memo propagates down each filing chain.
#
# Cost: rough ~$55-65 (technicals dominate). --max-llm-usd is a HARD cap. Re-runs cheap (warm cache).
#
# Run from repo root: bash scripts/run_ladder_sonnet45.sh
set -euo pipefail

uv run python -m src.backtest.score_all \
  --model claude-sonnet-4-5-20250929 \
  --sentiment --hybrid-sentiment \
  --horizon 20 --spacing 5 \
  --max-llm-usd 70 \
  --dates \
    2025-08-01 2025-08-08 2025-08-15 2025-08-22 2025-08-29 2025-09-05 2025-09-12 2025-09-19 \
    2025-09-26 2025-10-03 2025-10-10 2025-10-17 2025-10-24 2025-10-31 2025-11-07 2025-11-14 \
    2025-11-21 2025-11-28 2025-12-05 2025-12-12 2025-12-19 2025-12-26 2026-01-02 2026-01-09 \
    2026-01-16 2026-01-23 2026-01-30 2026-02-06 2026-02-13 2026-02-20 2026-02-27 2026-03-06 \
    2026-03-13 2026-03-20 2026-03-27 2026-04-03 2026-04-10 2026-04-17 2026-04-24
