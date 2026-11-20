#!/usr/bin/env bash
# serve/run_api.sh -- keep the submission API (serve_api.py) up.
#
#   ./serve/run_api.sh start     # start in the background, detached from this terminal
#   ./serve/run_api.sh stop      # stop the watchdog and the API
#   ./serve/run_api.sh status
#
# Why it exists: Radeon Cloud's rc-tunnel probes 127.0.0.1:7860 every 10 s and
# revokes the tunnel after about a minute of failed probes. The public URL is
# then gone for good, and a restarted service does not get it back. So the API
# should not be down for long: this script restarts a proven-idle API within
# two seconds. Uncertain in-flight work stops the restart loop for investigation.
# It runs under setsid so closing the terminal or
# notebook that started it does not take it down.
#
# Start serve/start.sh (the inference service) first, then this, then
# rc-tunnel expose. FPGACHINA_TOKEN and MODEL_NAME must be set in the
# environment, e.g. by `source /workspace/env.sh`.
#
# stop closes admission and waits for actual jobs/connections before child exit;
# a 10-second CLI wait can report stop pending without cancelling accepted work.
# The model service remains running after API stop. Do not use `set -u` here.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${API_PORT:-7860}"
umask 077
STATE="${RTL_API_STATE_DIR:-/workspace/v4_submission/runtime/run_api}"
PIDFILE="$STATE/run_api.pid"
LOG="$STATE/serve_api.log"
mkdir -p "$STATE"
IDENTITY="$STATE/run_api.start_ticks"

start_ticks() { [ -r "/proc/$1/stat" ] && sed 's/.*) //' "/proc/$1/stat" | awk '{print $20}'; }
# Z/X/x have exited even if their numeric PID has not been reaped yet.
live_start_ticks() {
    [ -r "/proc/$1/stat" ] || return 1
    sed 's/.*) //' "/proc/$1/stat" | awk '
        NF < 20 || $1 ~ /^[ZXx]$/ || $20 !~ /^[0-9]+$/ { exit 1 }
        { print $20 }
    '
}
running() {
    [ -r "$PIDFILE" ] && [ -r "$IDENTITY" ] || return 1
    local pid; pid="$(cat "$PIDFILE")"
    case "$pid" in ''|*[!0-9]*) return 1;; esac
    local current expected
    current="$(live_start_ticks "$pid")" || return 1
    expected="$(cat "$IDENTITY")"
    case "$current:$expected" in *[!0-9:]*|:*|*:) return 1;; esac
    [ "$current" = "$expected" ] && kill -0 "$pid" 2>/dev/null &&
        [ "$(live_start_ticks "$pid")" = "$expected" ]
}

case "${1:-}" in
start)
    if running; then
        echo "run_api.sh: already running (pid $(cat "$PIDFILE")), log $LOG"
        exit 0
    fi
    [ -n "$FPGACHINA_TOKEN" ] || { echo "run_api.sh: FPGACHINA_TOKEN is not set" >&2; exit 1; }
    # Bind the API's inherited environment as well as the inference child.
    source "$HERE/hip_runtime_env.sh"
    rtl_prepare_hip_runtime_environment "$(cd -- "$HERE/.." && pwd -P)"
    "$HERE/start.sh" || exit $?
    setsid "$0" _loop </dev/null >>"$LOG" 2>&1 &
    sleep 1
    running || { echo "run_api.sh: supervisor failed to start; inspect $LOG" >&2; exit 1; }
    echo "run_api.sh: started (pid $(cat "$PIDFILE" 2>/dev/null)), log $LOG"
    ;;
_loop)
    self_ticks="$(start_ticks "$$")"
    case "$self_ticks" in ''|*[!0-9]*) echo 'run_api.sh: cannot bind supervisor identity' >&2; exit 78;; esac
    echo "$self_ticks" > "$IDENTITY"
    echo $$ > "$PIDFILE"
    child=''
    stop_loop() {
        # Only an unreaped direct child can remain ours. Never signal a cached
        # PID during the restart delay after wait has reaped that child.
        if [ -n "$child" ] && [ -r "/proc/$child/status" ] &&
           [ "$(awk '/^PPid:/ {print $2}' "/proc/$child/status")" = "$$" ]; then
            kill "$child" 2>/dev/null
            wait "$child" 2>/dev/null
        fi
        rm -f "$PIDFILE" "$IDENTITY"
        exit 0
    }
    trap stop_loop TERM INT
    cd "$HERE/.." || exit 78
    while :; do
        echo "$(date '+%F %T') run_api.sh: starting serve_api on 127.0.0.1:$PORT"
        "${RTL_MODEL_LED_PYTHON:-python3}" -m official_entry.server --config "${RTL_MODEL_LED_CONFIG:-$HERE/../runtime_config.json}" &
        child=$!
        api_pid="$child"
        api_ticks="$(start_ticks "$child")"
        wait "$child"
        rc=$?
        child=''
        # The original parent has reaped this exact child. Restart only if its
        # durable receipt proves no accepted task/connection was in progress.
        # Unknown work is preserved, never replayed or signalled by a new API.
        if ! "${RTL_MODEL_LED_PYTHON:-python3}" -m official_entry.api_lifecycle "$api_pid" "$api_ticks" "$STATE"; then
            echo "run_api.sh: API exit has no confirmed idle receipt; STOP without restart" >&2
            rm -f "$PIDFILE" "$IDENTITY"
            exit 78
        fi
        echo "$(date '+%F %T') run_api.sh: idle API exited (rc=$rc), restarting in 2 s"
        sleep 2
    done
    ;;
stop)
    if running; then
        kill "$(cat "$PIDFILE")"
        for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 1; done
        running && { echo "run_api.sh: stop pending; actual accepted jobs/connections must drain; no forced kill" >&2; exit 1; }
        echo "run_api.sh: stopped"
    else
        echo "run_api.sh: not running"
    fi
    ;;
status)
    if running; then echo "run_api.sh: running (pid $(cat "$PIDFILE")), log $LOG"
    else echo "run_api.sh: not running"; exit 1; fi
    ;;
*)
    echo "usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac
