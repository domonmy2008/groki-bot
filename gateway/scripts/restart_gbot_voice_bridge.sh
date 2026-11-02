#!/usr/bin/env bash
# Restart gbot_voice_session_bridge.mjs with env from gateway/.env (never prints secrets).
# Used by run_stackchan_gateway_launchd.sh so a gateway-only restart cannot leave a stale bridge.
set -euo pipefail

GATEWAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${GATEWAY_DIR}/.." && pwd)"
BRIDGE_SCRIPT="${GATEWAY_DIR}/scripts/gbot_voice_session_bridge.mjs"
LOG_DIR="${REPO_ROOT}/.local/logs"
PID_FILE="${LOG_DIR}/gbot_voice_bridge.pid"
LOG_FILE="${LOG_DIR}/gbot_voice_bridge.log"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"

if [[ ! -f "${BRIDGE_SCRIPT}" ]]; then
    echo "warning: gbot voice bridge script missing (${BRIDGE_SCRIPT}); skipping bridge restart" >&2
    exit 0
fi

if [[ -z "${NODE_BIN}" ]]; then
    echo "warning: node not found on PATH; skipping gbot voice bridge restart" >&2
    exit 0
fi

# Export gateway/.env into this process. Do not echo values (secrets).
if [[ -f "${GATEWAY_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${GATEWAY_DIR}/.env"
    set +a
fi

mkdir -p "${LOG_DIR}"

stop_pid() {
    local pid="$1"
    [[ -n "${pid}" ]] || return 0
    if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
        local _i
        for _i in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "${pid}" 2>/dev/null || return 0
            sleep 0.1
        done
        kill -KILL "${pid}" 2>/dev/null || true
    fi
}

# Prefer the recorded pid; also clear any leftover process for this script.
if [[ -f "${PID_FILE}" ]]; then
    stop_pid "$(tr -d '[:space:]' < "${PID_FILE}" 2>/dev/null || true)"
    rm -f "${PID_FILE}"
fi
# pkill exits 1 when nothing matches; ignore that under set -e.
pkill -f "${BRIDGE_SCRIPT}" 2>/dev/null || true
sleep 0.2

cd "${GATEWAY_DIR}"
# Env already exported (STACKCHAN_GBOT_VOICE_BOT_ID / STACKCHAN_TOOL_BOT / …).
nohup "${NODE_BIN}" "${BRIDGE_SCRIPT}" >>"${LOG_FILE}" 2>&1 &
echo $! >"${PID_FILE}"
echo "gbot voice bridge restarted pid=$(tr -d '[:space:]' < "${PID_FILE}") log=${LOG_FILE}"
