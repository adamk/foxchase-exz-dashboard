#!/bin/zsh

# Start the local Alpaca connector and the EXZ browser dashboard together.
# Alpaca credentials remain in this terminal's environment and are never
# written to the repository or sent to the browser.

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
CONNECTOR_PORT="${ZWAP_CONNECTOR_PORT:-8789}"
WEB_PORT=8791
CONNECTOR_LOG="${TMPDIR:-/tmp}/foxchase-exz-connector.log"
WEB_LOG="${TMPDIR:-/tmp}/foxchase-exz-web.log"

cd "$SCRIPT_DIR"

if [[ -z "${APCA_API_KEY_ID:-}" || -z "${APCA_API_SECRET_KEY:-}" ]]; then
  print -u2 "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in this terminal first."
  exit 1
fi

if [[ ! -f config.js ]]; then
  print -u2 "Missing config.js. Copy config.example.js to config.js and review it first."
  exit 1
fi

if curl -fsS "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1; then
  print "Foxchase EXZ is already running at http://127.0.0.1:${WEB_PORT}/"
  if (( $+commands[open] )); then
    open "http://127.0.0.1:${WEB_PORT}/"
  fi
  exit 0
fi

cleanup() {
  trap - INT TERM EXIT
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  [[ -n "${CONNECTOR_PID:-}" ]] && kill "$CONNECTOR_PID" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

if curl -fsS "http://127.0.0.1:${CONNECTOR_PORT}/healthz" >/dev/null 2>&1; then
  print "Reusing the existing local Alpaca connector on port ${CONNECTOR_PORT}."
  CONNECTOR_PID=""
else
  if lsof -nP -iTCP:"$CONNECTOR_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    print -u2 "Port $CONNECTOR_PORT is already in use by another process."
    exit 1
  fi
  python3 local_connector.py >"$CONNECTOR_LOG" 2>&1 &
  CONNECTOR_PID=$!
fi

if lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  print -u2 "Port $WEB_PORT is already in use by another process."
  exit 1
fi

python3 -m http.server "$WEB_PORT" --bind 127.0.0.1 >"$WEB_LOG" 2>&1 &
WEB_PID=$!

for _ in {1..40}; do
  if curl -fsS "http://127.0.0.1:${CONNECTOR_PORT}/healthz" >/dev/null 2>&1 \
     && curl -fsSI "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! curl -fsS "http://127.0.0.1:${CONNECTOR_PORT}/healthz" >/dev/null 2>&1; then
  print -u2 "The local connector did not start. See $CONNECTOR_LOG"
  exit 1
fi

print "Foxchase EXZ is running at http://127.0.0.1:${WEB_PORT}/"
print "Press Ctrl-C to stop both local processes."
print "Connector log: $CONNECTOR_LOG"
print "Web log: $WEB_LOG"

if (( $+commands[open] )); then
  open "http://127.0.0.1:${WEB_PORT}/"
fi

wait "$WEB_PID"
