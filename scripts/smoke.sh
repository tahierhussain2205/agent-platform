#!/usr/bin/env bash
# Smoke check of the running local stack (`make dev` first). Exits non zero on the first failure.
set -euo pipefail

API_URL=${API_URL:-http://localhost:8000}
WEB_URL=${WEB_URL:-http://localhost:5173}
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

echo "Smoke checking the local stack"

body=$(curl -fsS "$API_URL/healthz") && [[ $body == '{"status":"ok"}' ]] && pass "api /healthz" || fail "api /healthz ($body)"

# Readiness may lag a fresh start (pools warming up while the host is busy building): wait up to 30 s.
ready='{"status":"ready","checks":{"database":"ok","key_value":"ok"}}'
for _ in $(seq 1 30); do
  body=$(curl -sS -m 5 "$API_URL/readyz" || true)
  echo "$body" | jq -e --argjson want "$ready" '. == $want' >/dev/null 2>&1 && break
  sleep 1
done
echo "$body" | jq -e --argjson want "$ready" '. == $want' >/dev/null 2>&1 \
  && pass "api /readyz (Postgres through PgBouncer as api_service, Key Value)" || fail "api /readyz ($body)"

rid="smoke-$(date +%s%N)"
curl -s -o /dev/null -H "x-request-id: $rid" "$API_URL/smoke-not-found"
line=$(docker compose logs api --no-log-prefix 2>/dev/null | grep "\"$rid\"" | tail -1)
echo "$line" | jq -e --arg rid "$rid" '.request_id == $rid and .http_status == 404 and .service == "agent-api" and (.trace_id | length == 32)' >/dev/null \
  && pass "structured JSON access log with request and trace ids" || fail "access log line: $line"

# The same request's trace (id from its access log line) must reach Jaeger (v3 query API).
trace_id=$(echo "$line" | jq -r .trace_id)
found=""
for _ in $(seq 1 20); do
  found=$(curl -fsS "http://localhost:16686/api/v3/traces/$trace_id" 2>/dev/null \
    | jq -r '[.result.resourceSpans[].scopeSpans[].spans[].name] | join(",")' 2>/dev/null) && [[ -n $found ]] && break
  sleep 1
done
[[ -n $found ]] && pass "request trace $trace_id exported to Jaeger ($found)" || fail "trace $trace_id not in Jaeger"

# Last line only: connection retries during startup may log before the answer.
# One retry: a ping started while a worker was being replaced can wait out its first attempt.
ping() { docker compose exec -T worker python -m app check temporal 2>&1 | tail -1; }
out=$(ping); [[ $out == "pong: smoke" ]] || out=$(ping)
[[ $out == "pong: smoke" ]] \
  && pass "worker connected to Temporal (PingWorkflow -> $out)" || fail "PingWorkflow: $out"

for svc in api worker; do
  out=$(docker compose exec -T "$svc" python -m app check vault 2>&1 | tail -1) && [[ $out == "vault: ready" ]] \
    && pass "$svc reaches the token vault with the service token" || fail "$svc -> vault: $out"
done
# The vault is on the private network only: nothing is published on the host.
[[ -z $(docker compose port vault 8100 2>/dev/null) ]] && pass "vault not published on the host" || fail "vault port published"

curl -fsS "$WEB_URL/" | grep -q '<div id="root">' && pass "web console served" || fail "web console"

curl -fsS -o /dev/null http://localhost:8233/ && pass "Temporal UI" || fail "Temporal UI"
curl -fsS -o /dev/null http://localhost:16686/ && pass "Jaeger UI" || fail "Jaeger UI"
# Anonymous S3 requests get 403 whether or not a bucket exists, so ask the server's admin shell.
echo "s3.bucket.list" | docker compose exec -T objectstore weed shell 2>/dev/null | grep -qE '^\s*artifacts\s' \
  && pass "object store has the artifacts bucket" || fail "object store bucket"

# The sandbox runner runs a provider command, with no network (names do not even resolve).
sbx_api=http://localhost:4100/sandbox/v1
sbx_auth="authorization: Bearer sbx-local-key"
sbx=$(curl -fsS -X POST -H "$sbx_auth" -H 'content-type: application/json' "$sbx_api/sandboxes" \
  -d '{"ttl_seconds":120,"limits":{"disk_mb":8},"metadata":{"purpose":"smoke"}}' | jq -r .id)
ran=$(curl -fsS -X POST -H "$sbx_auth" -H 'content-type: application/json' "$sbx_api/sandboxes/$sbx/exec" \
  -d '{"command":"echo ran; getent hosts api || echo no-network","timeout_seconds":20}' | jq -r .stdout)
curl -fsS -o /dev/null -X DELETE -H "$sbx_auth" "$sbx_api/sandboxes/$sbx"
[[ $ran == $'ran\nno-network' ]] && pass "sandbox runner runs commands with no network" || fail "sandbox runner: $ran"

psql_q() { docker compose exec -T postgres psql -U postgres -d agent -tAc "$1"; }
[[ $(psql_q "select count(*) from public.schema_migrations where version = '001'") == 1 ]] \
  && pass "migration 001 recorded" || fail "migration 001 not recorded"
[[ $(psql_q "select count(*) from app.orgs where clerk_org_id in ('org_acme', 'org_globex')") == 2 ]] \
  && pass "seed orgs present" || fail "seed orgs missing"

echo "All smoke checks passed"
