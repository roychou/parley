#!/usr/bin/env bash
#
# Weekly forward paper-trading session — the hands-off clock tick.
#
# Invoked by launchd (see com.parley.forward-weekly.plist) or run by hand. Runs one
# `src.forward.run` session, logging full output to a dated file under
# data/forward/logs/ and a one-line status to stdout (which launchd captures).
#
# Configurable via environment (set in the plist, or inline for a manual run):
#   PARLEY_MAX_LLM_USD   hard LLM spend cap for the session (default 8)
#   PARLEY_ARGS          extra args passed through to src.forward.run (e.g. --no-news)
#
# Prices + news come from IB Gateway/TWS, so the Gateway must be up when this runs.
# launchd runs with a minimal environment, so everything here is absolute.

set -uo pipefail

REPO="/Users/roychou/Development/parley"
UV="/Users/roychou/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv || true)"

CAP="${PARLEY_MAX_LLM_USD:-8}"
EXTRA="${PARLEY_ARGS:-}"

LOG_DIR="$REPO/data/forward/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG="$LOG_DIR/forward_$STAMP.log"

echo "[$(date)] forward weekly: cap=\$$CAP -> $LOG"
cd "$REPO" || { echo "[$(date)] FATAL: cannot cd $REPO"; exit 1; }
if [ -z "$UV" ]; then echo "[$(date)] FATAL: uv not found"; exit 1; fi

# shellcheck disable=SC2086
"$UV" run python -m src.forward.run --max-llm-usd "$CAP" $EXTRA \
    >> "$LOG" 2>&1
rc=$?

if [ $rc -eq 0 ]; then
    echo "[$(date)] OK (exit 0). Digest: $REPO/data/forward/digests/"
else
    echo "[$(date)] FAILED (exit $rc) — see $LOG (Gateway down? out of credits? check the tail)"
fi
exit $rc
