#!/usr/bin/env bash
# Smoke check of the running local stack (`make dev` first). Exits non zero on the first failure.
set -euo pipefail

API_URL=${API_URL:-http://localhost:8000}
WEB_URL=${WEB_URL:-http://localhost:5173}
pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

echo "Smoke checking the local stack"

body=$(curl -fsS "$API_URL/healthz") && [[ $body == '{"status":"ok"}' ]] && pass "api /healthz" || fail "api /healthz ($body)"

out=$(docker compose exec -T worker python -m app check temporal 2>&1) && [[ $out == "pong: smoke" ]] \
  && pass "worker connected to Temporal (PingWorkflow -> $out)" || fail "PingWorkflow: $out"

curl -fsS "$WEB_URL/" | grep -q '<div id="root">' && pass "web console served" || fail "web console"

curl -fsS -o /dev/null http://localhost:8233/ && pass "Temporal UI" || fail "Temporal UI"
curl -fsS -o /dev/null http://localhost:16686/ && pass "Jaeger UI" || fail "Jaeger UI"
# Anonymous S3 requests get 403 whether or not a bucket exists, so ask the server's admin shell.
echo "s3.bucket.list" | docker compose exec -T objectstore weed shell 2>/dev/null | grep -qE '^\s*artifacts\s' \
  && pass "object store has the artifacts bucket" || fail "object store bucket"

psql_q() { docker compose exec -T postgres psql -U postgres -d agent -tAc "$1"; }
[[ $(psql_q "select count(*) from public.schema_migrations where version = '001'") == 1 ]] \
  && pass "migration 001 recorded" || fail "migration 001 not recorded"
[[ $(psql_q "select string_agg(name, ',' order by name) from app.orgs") == "Acme Corp,Globex Inc" ]] \
  && pass "seed orgs present" || fail "seed orgs missing"

echo "All smoke checks passed"
