#!/usr/bin/env bash
#
# Gateway container entrypoint: render IBC config from env, start a virtual display,
# relay the API off localhost, then hand off to IBC (which logs Gateway in and keeps it
# logged in across the daily restart). PID 1 is IBC so the container lives/dies with it.
set -euo pipefail

# --- required env (fail fast + clearly if missing) ---
: "${TWS_USERID:?set TWS_USERID (your IBKR paper username)}"
: "${TWS_PASSWORD:?set TWS_PASSWORD (your IBKR paper password)}"
TRADING_MODE="${TRADING_MODE:-paper}"
API_PORT_INTERNAL="${API_PORT_INTERNAL:-4002}"   # Gateway's own API port (paper = 4002)
API_PORT_RELAY="${API_PORT_RELAY:-4004}"         # exposed to the compose network

if [ "$TRADING_MODE" != "paper" ]; then
    # Guardrail: this stack is for the paper forward clock. Live is a deliberate,
    # separate decision (real money + ReadOnlyApi=no), not a config typo.
    echo "FATAL: TRADING_MODE=$TRADING_MODE — this image refuses anything but 'paper'." >&2
    exit 1
fi

# 1. Render IBC config (creds injected here, never in the image). 0600 — has a password.
export TWS_USERID TWS_PASSWORD TRADING_MODE API_PORT_INTERNAL
envsubst < /opt/ibc/config.ini.tmpl > /opt/ibc/config.ini
chmod 600 /opt/ibc/config.ini

# 2. Virtual framebuffer — Gateway is a GUI app; a server has no real X.
rm -f /tmp/.X0-lock
Xvfb :0 -screen 0 1024x768x16 -nolisten tcp &
export DISPLAY=:0

# 3. Relay 0.0.0.0:RELAY -> 127.0.0.1:INTERNAL so the parley container can reach the API
#    while Gateway keeps its safe localhost-only binding. (fork = one child per client.)
socat TCP-LISTEN:"${API_PORT_RELAY}",fork,reuseaddr TCP:127.0.0.1:"${API_PORT_INTERNAL}" &

# 4. Launch Gateway under IBC.
#    VERIFY: gatewaystart.sh is IBC 3.x's launcher; env below is the documented contract.
#    If your IBC version wants the TWS major version as an arg, pass it (TWS_MAJOR_VRSN).
export TWS_PATH=/opt/ibgateway
export IBC_PATH=/opt/ibc
export IBC_INI=/opt/ibc/config.ini
export LOG_PATH=/opt/ibc/logs
export TWOFA_TIMEOUT_ACTION=restart
mkdir -p "$LOG_PATH"

echo "[entrypoint] starting IBC (mode=$TRADING_MODE, api relay 0.0.0.0:${API_PORT_RELAY} -> 127.0.0.1:${API_PORT_INTERNAL})"
exec /opt/ibc/gatewaystart.sh --gateway
