#!/usr/bin/env bash
# Checks the backend image behaves as Render needs: non root, honors $PORT, stops cleanly on
# SIGTERM. Needs the image built (`make dev` or `docker compose build`) and, for the worker check,
# the Compose Temporal running.
set -euo pipefail

IMAGE=${IMAGE:-agentplatform-backend:local}
NETWORK=${NETWORK:-agentplatform_default}
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
cleanup() { docker rm -f img-test-api img-test-worker >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "Checking image $IMAGE"

docker run -d --name img-test-api -e PORT=9122 "$IMAGE" >/dev/null
for _ in $(seq 1 50); do docker exec img-test-api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9122/healthz')" 2>/dev/null && break; sleep 0.2; done
docker exec img-test-api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9122/healthz')" \
  && pass "default command starts the api" || fail "default command"
docker rm -f img-test-api >/dev/null

[[ $(docker run --rm --entrypoint id "$IMAGE" -u) == 10001 ]] && pass "runs as uid 10001" || fail "runs as root"
docker run --rm --entrypoint sh "$IMAGE" -c 'test ! -e /app/tests && test ! -e /app/.env' \
  && pass "no tests or .env in the image" || fail "tests or .env baked into the image"
set +e; out=$(docker run --rm "$IMAGE" app maintenance 2>&1); rc=$?; set -e
[[ $rc != 0 ]] && grep -q "DATABASE_URL is not set" <<<"$out" \
  && pass "unconfigured command fails loudly (exit $rc)" || fail "unconfigured maintenance: exit $rc"

# A stopped container's last log lines can reach `docker logs` a moment after `docker stop` returns
# (seen once under load): look for a line for up to 2 seconds.
logged() {
  for _ in $(seq 1 10); do docker logs "$1" 2>&1 | grep -q "$2" && return 0; sleep 0.2; done
  return 1
}

# API: custom PORT, health, graceful stop well within the grace period.
docker run -d --name img-test-api -e PORT=9123 -p 9123:9123 "$IMAGE" app api >/dev/null
for _ in $(seq 1 50); do curl -fsS localhost:9123/healthz >/dev/null 2>&1 && break; sleep 0.2; done
curl -fsS localhost:9123/healthz | grep -q ok && pass "api listens on \$PORT=9123" || fail "api not on 9123"
start=$(date +%s); docker stop -t 30 img-test-api >/dev/null; took=$(( $(date +%s) - start ))
code=$(docker inspect -f '{{.State.ExitCode}}' img-test-api)
logged img-test-api "Application shutdown complete" && (( took < 10 )) \
  && pass "api drains and exits on SIGTERM in ${took}s (exit $code)" || fail "api shutdown (took ${took}s, exit $code)"

# Worker: connects to Temporal, stops polling and exits 0 on SIGTERM.
docker run -d --name img-test-worker --network "$NETWORK" -e TEMPORAL_ADDRESS=temporal:7233 \
  -e TEMPORAL_TASK_QUEUE=img-test -e WORKER_GRACEFUL_SHUTDOWN_SECONDS=5 "$IMAGE" app worker >/dev/null
for _ in $(seq 1 60); do docker logs img-test-worker 2>&1 | grep -q "worker polling" && break; sleep 0.5; done
docker logs img-test-worker 2>&1 | grep -q "worker polling" && pass "worker connects to Temporal" || fail "worker did not connect"
start=$(date +%s); docker stop -t 30 img-test-worker >/dev/null; took=$(( $(date +%s) - start ))
code=$(docker inspect -f '{{.State.ExitCode}}' img-test-worker)
[[ $code == 0 ]] && logged img-test-worker "worker stopped" && (( took < 15 )) \
  && pass "worker exits 0 on SIGTERM in ${took}s" || fail "worker shutdown (exit $code, took ${took}s)"

echo "Image checks passed"
