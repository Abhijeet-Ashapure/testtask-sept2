#!/bin/bash
# Reference (oracle) solution for the firmware release publisher task.
# Harbor copies this directory to /solution and runs: bash /solution/publish.sh
# The script installs the reference publisher into the agent workdir and executes
# the graded entry point so the verifier sees a correct end state.
set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"
SOLUTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${APP_ROOT}/publisher"
cp "${SOLUTION_DIR}/release-publisher.mjs" \
  "${APP_ROOT}/publisher/release-publisher.mjs"

# Reset prior publisher state so the oracle run is deterministic.
rm -f "${APP_ROOT}/releases.duckdb" "${APP_ROOT}/releases.duckdb.wal"

# Ensure the distribution gateway is up (verifier may also start it; tolerate
# an already-running process on 7070).
GATEWAY_PID=""
if ! curl -sf "http://127.0.0.1:7070/healthz" >/dev/null 2>&1; then
  (
    cd "${APP_ROOT}/distribution-gateway"
    node server.js
  ) >/tmp/distribution-gateway.oracle.log 2>&1 &
  GATEWAY_PID=$!
  for _ in $(seq 1 50); do
    if curl -sf "http://127.0.0.1:7070/healthz" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  if ! curl -sf "http://127.0.0.1:7070/healthz" >/dev/null 2>&1; then
    echo "distribution-gateway failed to become ready" >&2
    exit 1
  fi
fi

cd "${APP_ROOT}"
npm run report

# Leave the gateway running if the verifier started it; only stop our own child.
if [[ -n "${GATEWAY_PID}" ]]; then
  kill "${GATEWAY_PID}" >/dev/null 2>&1 || true
fi
