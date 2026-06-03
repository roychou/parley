#!/usr/bin/env bash
#
# parley app entrypoint. Ensures the state dirs exist on the mounted volumes, then:
#   - no args  -> run supercronic (the scheduled weekly session + daily watchdog), or
#   - args     -> run them directly, e.g. a one-off manual session:
#                 docker compose run --rm parley python -m src.forward.run --as-of 2026-06-07
set -euo pipefail

mkdir -p /app/data/forward/logs /app/data/cache

# Optional dividend seed: if you rsync'd an FMP-derived dividend cache to /seed/fmp_signals
# (it's gitignored, so not baked), copy it into the cache volume once, without clobbering.
if [ -d /seed/fmp_signals ]; then
    mkdir -p /app/data/cache/fmp_signals
    cp -rn /seed/fmp_signals/. /app/data/cache/fmp_signals/ 2>/dev/null || true
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec supercronic /app/deploy/app/crontab
