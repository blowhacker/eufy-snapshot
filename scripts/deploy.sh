#!/usr/bin/env bash
#
# Deploy wanyard to the GPU host (banana): push -> pull -> build -> recreate,
# then VERIFY the GPU actually reached the containers.
#
# Why this exists: the GPU device reservation for the yolo detector and the
# stamper's NVENC lives only in docker-compose.gpu.yml (an overlay). Two ways a
# hand-run deploy silently drops it and pins ~540% CPU on yolo (CPU inference):
#   1. `docker compose restart` reuses the existing container config and never
#      re-applies the device reservation.
#   2. A deploy that forgets the gpu overlay (stale/edited .env COMPOSE_FILE)
#      recreates yolo/stamper without GPUs.
# This script removes both foot-guns: it always builds COMPOSE_FILE with the gpu
# overlay itself (independent of .env), always uses `up -d`, never `restart`, and
# fails the deploy if torch/CUDA or the device reservation is missing afterward.
#
# Usage:   scripts/deploy.sh
# Env:     WANYARD_DEPLOY_HOST (default: banana)
#          WANYARD_DEPLOY_DIR  (default: ~/work/wanyard)
set -euo pipefail

HOST="${WANYARD_DEPLOY_HOST:-banana}"
REMOTE_DIR="${WANYARD_DEPLOY_DIR:-~/work/wanyard}"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
  echo "FATAL: detached HEAD — check out a branch before deploying." >&2
  exit 1
fi

echo ">> pushing $BRANCH to origin"
git push origin "$BRANCH"

echo ">> deploying $BRANCH on $HOST:$REMOTE_DIR"
# Pass locals as an env prefix to the remote shell; the script body is a quoted
# heredoc (no local interpolation) so it runs verbatim on the host.
ssh "$HOST" "BRANCH=$(printf %q "$BRANCH") REMOTE_DIR=$(printf %q "$REMOTE_DIR") bash -s" <<'REMOTE'
set -euo pipefail
cd "${REMOTE_DIR/#\~/$HOME}"

echo ">> pulling $BRANCH"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

# Build COMPOSE_FILE ourselves: base + local override (ports, per-host) + the
# GPU overlay, which is mandatory. Setting it here overrides whatever COMPOSE_FILE
# a drifted .env carries, while .env is still loaded for variable substitution.
if [ ! -f docker-compose.gpu.yml ]; then
  echo "FATAL: docker-compose.gpu.yml missing — refusing to deploy without the GPU overlay." >&2
  exit 1
fi
files="docker-compose.yml"
[ -f docker-compose.override.yml ] && files="$files:docker-compose.override.yml"
files="$files:docker-compose.gpu.yml"
export COMPOSE_FILE="$files"
echo ">> COMPOSE_FILE=$COMPOSE_FILE"

# up -d (re)creates containers so the device reservation is applied. `restart`
# would not — that is exactly the bug this script prevents.
echo ">> building + recreating"
docker compose up -d --build

echo ">> verifying GPU passthrough"
fail=0
for svc in yolo stamper; do
  cid="$(docker compose ps -q "$svc" || true)"
  if [ -z "$cid" ]; then
    echo "  ! $svc: no container" >&2; fail=1; continue
  fi
  dr="$(docker inspect "$cid" --format '{{json .HostConfig.DeviceRequests}}')"
  if [ "$dr" = "null" ] || [ -z "$dr" ]; then
    echo "  ! $svc: no GPU device reservation (DeviceRequests=null)" >&2; fail=1
  else
    echo "  ok $svc: device reservation present"
  fi
done

# yolo can have the device but still run on CPU if torch can't see CUDA. Retry:
# the container needs a few seconds after (re)create before python is up.
# </dev/null is load-bearing: this whole script arrives on bash's stdin (bash -s
# heredoc), and docker compose exec inherits that stdin — without the redirect
# it EATS the remainder of the script, silently skipping every check below.
cuda_ok=0
for _ in 1 2 3 4 5 6; do
  if docker compose exec -T yolo python3 -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' </dev/null 2>/dev/null; then
    cuda_ok=1; break
  fi
  sleep 5
done
if [ "$cuda_ok" = 1 ]; then
  echo "  ok yolo: torch.cuda.is_available()=True"
else
  echo "  ! yolo: torch.cuda.is_available()=False — inference would run on CPU" >&2; fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "DEPLOY FAILED: GPU not fully wired. Fix docker-compose.gpu.yml / .env and re-run." >&2
  exit 1
fi
echo ">> deploy OK — GPU wired for yolo + stamper"
REMOTE

echo ">> done"
