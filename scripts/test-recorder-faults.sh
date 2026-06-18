#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE_FILE="$ROOT/docker-compose.recorder-tests.yml"
PROJECT_NAME="wanyard-recorder-tests-$$"

compose() {
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT HUP INT TERM
cleanup
compose up --build --abort-on-container-exit --exit-code-from recorder-tests
