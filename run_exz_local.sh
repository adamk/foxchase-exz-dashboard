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

stop_listener() {
  local port="$1"
  local label="$2"
  local pid process_cwd

  pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
  if [[ -z "$pid" ]]; then
    return 0
  fi

  process_cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  if [[ "$process_cwd" != "$SCRIPT_DIR" ]]; then
    print -u2 "Port ${port} is occupied outside the EXZ directory; not stopping PID ${pid}: ${process_cwd:-unknown directory}"
    return 1
  fi

  print "Stopping ${label} (PID ${pid})…"
  if ! kill "$pid"; then
    print -u2 "Unable to signal ${label} (PID ${pid})."
    return 1
  fi
  for _ in {1..30}; do
    ! kill -0 "$pid" 2>/dev/null && return 0
    sleep 0.1
  done
  print -u2 "${label} did not stop cleanly (PID ${pid})."
  return 1
}

if [[ "${1:-}" == "stop" ]]; then
  failed=0
  stop_listener "$WEB_PORT" "EXZ web server" || failed=1
  stop_listener "$CONNECTOR_PORT" "EXZ Alpaca connector" || failed=1

  if (( failed )); then
    print -u2 "Foxchase EXZ could not be stopped completely."
    exit 1
  fi
  print "Foxchase EXZ is stopped."
  exit 0
fi

if (( $# > 0 )); then
  print -u2 "Usage: ./run_exz_local.sh [stop]"
  exit 2
fi

# Reuse the existing local Alpaca environment without sourcing unrelated
# application settings. Only known Alpaca variable names are imported.
ENV_FILE="${ZWAP_ENV_FILE:-}"
if [[ -z "$ENV_FILE" && -f "$SCRIPT_DIR/.env" ]]; then
  ENV_FILE="$SCRIPT_DIR/.env"
fi
if [[ -z "$ENV_FILE" && -f "$HOME/.foxchase_alpaca_source.env" ]]; then
  ENV_FILE="$HOME/.foxchase_alpaca_source.env"
fi
if [[ -z "$ENV_FILE" && -f "$HOME/foxchasetrading.com/.env" ]]; then
  ENV_FILE="$HOME/foxchasetrading.com/.env"
fi
if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  while IFS='=' read -r name value; do
    case "$name" in
      ALPACA_API_KEY|ALPACA_API_SECRET|ALPACA_SECRET_KEY|ALPACA_KEY|ALPACA_SECRET)
        export "$name=$value" ;;
    esac
  done < "$ENV_FILE"
fi
export APCA_API_KEY_ID="${APCA_API_KEY_ID:-${ALPACA_API_KEY:-}}"
export APCA_API_SECRET_KEY="${APCA_API_SECRET_KEY:-${ALPACA_API_SECRET:-${ALPACA_SECRET_KEY:-}}}"

if [[ -z "${APCA_API_KEY_ID:-}" || -z "${APCA_API_SECRET_KEY:-}" ]]; then
  print -u2 "No Alpaca credentials found. Set APCA_API_KEY_ID/APCA_API_SECRET_KEY or configure $HOME/.foxchase_alpaca_source.env."
  exit 1
fi

if [[ ! -f config.js ]]; then
  print -u2 "Missing config.js. Copy config.example.js to config.js and review it first."
  exit 1
fi

if curl -fsS "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1 \
   && curl -fsS "http://127.0.0.1:${CONNECTOR_PORT}/healthz" >/dev/null 2>&1; then
  print "Foxchase EXZ is already running at http://127.0.0.1:${WEB_PORT}/"
  if (( $+commands[open] )); then
    open "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1 || true
  fi
  exit 0
fi

# A static web server can remain alive after the connector has exited, making
# the dashboard appear healthy while its data is frozen. Remove only our known
# local http.server process so a complete pair can be started below.
if curl -fsS "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1 \
   && ! curl -fsS "http://127.0.0.1:${CONNECTOR_PORT}/healthz" >/dev/null 2>&1; then
  STALE_WEB_PID="$(lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
  if [[ -n "$STALE_WEB_PID" ]] && ps -p "$STALE_WEB_PID" -o command= 2>/dev/null | grep -q "http.server ${WEB_PORT}"; then
    print "Removing stale EXZ web server (the data connector is offline)."
    kill "$STALE_WEB_PID"
    for _ in {1..20}; do
      ! kill -0 "$STALE_WEB_PID" 2>/dev/null && break
      sleep 0.1
    done
  fi
fi
if lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  print "Port ${WEB_PORT} is already occupied; leaving the existing dashboard process untouched."
  if (( $+commands[open] )); then
    open "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1 || true
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
    print "Port ${CONNECTOR_PORT} is already occupied; leaving the existing connector untouched."
    CONNECTOR_PID=""
  else
    python3 local_connector.py >"$CONNECTOR_LOG" 2>&1 &
    CONNECTOR_PID=$!
  fi
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
  open "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1 || true
fi

# Supervise both halves. If either process exits, cleanup stops the survivor
# instead of leaving an apparently live but frozen dashboard behind.
while true; do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    print -u2 "The EXZ web server stopped. See $WEB_LOG"
    exit 1
  fi
  if [[ -n "${CONNECTOR_PID:-}" ]] && ! kill -0 "$CONNECTOR_PID" 2>/dev/null; then
    print -u2 "The EXZ connector stopped. See $CONNECTOR_LOG"
    exit 1
  fi
  sleep 5
done
