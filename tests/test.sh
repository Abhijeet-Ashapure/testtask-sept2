#!/bin/bash
# Verifier entrypoint for the firmware release publisher task.
# Resets runtime state, starts the distribution gateway, runs pytest, and writes
# a binary 0/1 reward under /logs/verifier/reward.txt.
set -euo pipefail

if [ "$PWD" = "/" ]; then
  echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile before running this script."
  exit 1
fi

APP_ROOT="${APP_ROOT:-/app}"
LOG_DIR="${LOG_DIR:-/logs/verifier}"
mkdir -p "${LOG_DIR}"

# --- reset prior state --------------------------------------------------------
# Free the gateway port so we never attach to a leftover process that still
# holds publications in memory while its on-disk ledger was deleted.
if command -v lsof >/dev/null 2>&1; then
  lsof -ti:7070 | xargs kill -9 >/dev/null 2>&1 || true
elif command -v fuser >/dev/null 2>&1; then
  fuser -k 7070/tcp >/dev/null 2>&1 || true
fi
sleep 0.2

rm -f "${APP_ROOT}/releases.duckdb" "${APP_ROOT}/releases.duckdb.wal"
rm -f "${APP_ROOT}/distribution-gateway/data/gateway.json"

# --- start gateway ------------------------------------------------------------
GATEWAY_LOG="${LOG_DIR}/gateway.log"
(
  cd "${APP_ROOT}/distribution-gateway"
  exec node server.js
) >"${GATEWAY_LOG}" 2>&1 &
GATEWAY_PID=$!

cleanup() {
  kill "${GATEWAY_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ready=0
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:7070/healthz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.2
done

if [ "${ready}" -ne 1 ]; then
  echo "distribution-gateway failed to become ready" >&2
  cat "${GATEWAY_LOG}" >&2 || true
  echo 0 > "${LOG_DIR}/reward.txt"
  exit 1
fi

# --- run pytest ---------------------------------------------------------------
# Prefer Harbor mount (/tests), then path next to this script, then cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_FILE=""
if [ -f /tests/test_outputs.py ]; then
  TEST_FILE=/tests/test_outputs.py
elif [ -f "${SCRIPT_DIR}/test_outputs.py" ]; then
  TEST_FILE="${SCRIPT_DIR}/test_outputs.py"
elif [ -f tests/test_outputs.py ]; then
  TEST_FILE=tests/test_outputs.py
else
  echo "test_outputs.py not found" >&2
  echo 0 > "${LOG_DIR}/reward.txt"
  exit 2
fi

export APP_ROOT
export GATEWAY_URL="http://127.0.0.1:7070"
# Point openssl / gateway at local keys when not running inside the image.
if [ -z "${CURRENT_CERT_PATH:-}" ] && [ -f "${APP_ROOT}/keys/current/current.cert.pem" ]; then
  export CURRENT_CERT_PATH="${APP_ROOT}/keys/current/current.cert.pem"
fi

PY=python3
if ! command -v python3 >/dev/null 2>&1; then
  PY=python
fi

set +e
if "$PY" -c "import pytest_json_ctrf" >/dev/null 2>&1; then
  "$PY" -m pytest --ctrf "${LOG_DIR}/ctrf.json" "${TEST_FILE}" -rA
else
  "$PY" -m pytest "${TEST_FILE}" -rA
fi
code=$?
set -e

echo "pytest exit code: ${code}"

if [ "${code}" -eq 0 ]; then
  echo 1 > "${LOG_DIR}/reward.txt"
else
  echo 0 > "${LOG_DIR}/reward.txt"
fi

exit "${code}"
