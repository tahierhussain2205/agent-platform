# Phase 4: Backend core

**Goal:** the service layer every later phase builds on.

**Done when:** the middleware tests pass against Postgres through PgBouncer locally, and staging shows traces and logs per request (staging is blocked by B-001; locally, Jaeger shows traces and the API writes structured logs per request).

## Design

### Unit of work (spec 14.1, invariants 1 and 9)

A pure ASGI middleware owns one `UnitOfWork` per request (`request.state.uow`):

- **Lazy, single org binding.** `await uow.bind(org_id)` takes a pooled connection (as `api_service`), starts the transaction and runs `select set_config('app.org_id', $1, true)` as its first statement. Binding the same org again is a no op; binding a second org raises `OrgBindingError` (500, rolled back). Requests that never bind never take a connection.
- **Commit before the response starts.** The middleware wraps ASGI `send`: on `http.response.start` with status below 400 it commits first. If the commit fails (deferred constraints such as the primary role rule) the client gets the mapped error response instead of the handler's, and the handler's body is dropped. Status 400 or above, and exceptions, roll back.
- **Closed after the response starts.** Any later use of the unit of work (a background task, a streaming body) raises; background work must open its own.
- **Always clean.** The connection goes back to the pool after commit or rollback, so no transaction scoped setting survives; `set_config(..., true)` makes this safe under PgBouncer transaction pooling too.

The middleware sits outside FastAPI's exception handling, so handlers' mapped errors arrive as 4xx responses (rolled back) and unhandled exceptions are rolled back before Starlette turns them into 500s.

### Database errors to HTTP (spec 14.1)

| SQLSTATE | Meaning | HTTP |
|---|---|---|
| 42501 | insufficient privilege, row level security violation | 403 |
| 23505 | unique violation | 409 |
| 23503 | foreign key violation | 409 |
| 23514 | check violation | 422 |
| 23502 | not null violation | 422 |

Bodies are `{"error": {"code", "message", "request_id"}}` with a generic message per class and the constraint name when known. The raw database message (which can include values) is logged, not returned.

### Pools and PgBouncer

- `psycopg_pool.AsyncConnectionPool` per component with `prepare_threshold=None` (no server side prepared statements, plan rule 7), opened in the app lifespan, closed on shutdown.
- Compose gains `pgbouncer` (transaction mode, SCRAM, one userlist entry per component login user). The API and worker use it; migrations keep the direct connection.

### Logging

- JSON lines on stdout: `ts`, `level`, `logger`, `msg`, `request_id`, `org_id`, `trace_id`, `span_id`, plus extras. Context comes from `contextvars`, so it follows async tasks.
- One access log line per request (method, path template, status, duration). Uvicorn's own access log is disabled.
- A redaction filter scrubs bearer tokens, Slack tokens, API keys and URL passwords from every record as a backstop (invariant 5); code still never logs secrets.
- `X-Request-ID`: accepted from the caller if well formed, otherwise generated; echoed in the response.

### Tracing

- OpenTelemetry SDK; OTLP over HTTP to Jaeger locally (`OTEL_EXPORTER_OTLP_ENDPOINT`). Without an endpoint, tracing is a no op.
- ASGI server spans (send/receive sub spans excluded), psycopg spans (statement text only, never parameters).
- **Tenant ID on every span:** binding the unit of work sets `tenant.id` on the current server span and in a context variable; a span processor stamps it on every span started afterwards.
- Temporal client and worker get the OpenTelemetry interceptor.

### Key Value

`redis.asyncio` client from `REDIS_URL`. Helpers: `tenant_key(org_id, *parts)` (tenant on every cache key, spec 12.5), `claim(key, ttl)` / `extend` / `release` (set if absent, used by ingress dedupe in phase 6), `ping`.

### Envelope encryption

- `KeyProvider` interface: `active_key_id()`, `wrap(org_id, key_id, dek)`, `unwrap(org_id, key_id, wrapped)`.
- `LocalKeyProvider`: master keys from `ENCRYPTION_KEYS` (`v1:<base64 32 bytes>,v2:...`) and `ENCRYPTION_ACTIVE_KEY` (Render secret). A per tenant key encryption key is derived with HKDF from the master key and the org id, so each tenant has its own key without storing one. `kms_key_id` records `local:<version>`. A cloud KMS provider can implement the same interface later.
- Each secret gets a fresh 256 bit data key; AES-256-GCM for both the payload and the wrapped data key. Associated data binds the ciphertext to its org, its row and its purpose, so a ciphertext copied to another row or tenant does not decrypt.
- `rewrap()` moves a ciphertext to the active key version (rotation).

### Object storage

boto3 S3 client (in a thread) against `S3_ENDPOINT_URL` (SeaweedFS locally, S3 or R2 later). Keys are `tenants/<org_id>/<kind>/<uuid>`; reads and deletes check that a reference belongs to the caller's org before touching the store.

### Readiness

`GET /readyz` checks Postgres (as `api_service`) and Key Value with short timeouts; 200 with per check status, 503 if any fails. Messages never include connection strings. `/healthz` stays the process check Render uses.

## Tasks

1. Dependencies: OpenTelemetry (api, sdk, OTLP HTTP exporter, ASGI and psycopg instrumentation), `redis`, `cryptography`, `boto3` (+ stubs).
2. `app/context.py`, `app/logging.py`, `app/telemetry.py`.
3. `app/db/pool.py`, `app/db/uow.py` (middleware, `UnitOfWork`, dependency), `app/db/errors.py`.
4. `app/kv.py`, `app/temporal.py` (client factory shared by API and worker, tracing interceptor).
5. `app/crypto/` (provider interface, local provider, envelope).
6. `app/storage.py`.
7. `app/api/main.py`: lifespan (pools, KV, telemetry), middlewares (request context, unit of work), exception handlers, `/readyz`.
8. Compose: `pgbouncer`; API and worker on the pool; Jaeger endpoint; encryption and S3 settings. Render: new settings (`ENCRYPTION_KEYS` as `sync: false`, S3 settings).
9. Smoke: `/readyz` and a trace visible in Jaeger.

## Tests

| Area | Tests (real Postgres, real PgBouncer, real Valkey, real SeaweedFS) |
|---|---|
| Unit of work | commit happens before the response start message; deferred constraint failure returns 422 and nothing persists; 4xx and exceptions roll back; second org binding fails and rolls back; same org twice is fine; no connection taken without binding; use after response raises |
| Context isolation | with a one connection pool: org A request, then a request with no binding sees no org setting, then org B sees only B; repeated through PgBouncer; 60 concurrent requests across two orgs each see only their own rows |
| Error mapping | each SQLSTATE class to its status; body shape; raw message not returned |
| Logging | JSON shape, request and org ids, trace ids, access log, redaction of tokens and URL passwords, request id echo and validation |
| Tracing | tenant id on the server span and on database spans after binding; absent before; no parameters in spans |
| Encryption | round trip; other org, other row or other purpose fails; tampering fails; rotation (old key still decrypts, rewrap to active); unknown key version fails; bad key configuration rejected |
| Storage | round trip, tenant prefix, cross tenant reference refused, missing object error |
| Key Value | claim, extend, release semantics; tenant key requires an org |
| Readiness | ready with dependencies up; 503 with database or Key Value down; no secrets in the body |
| Stack | `/readyz` through Compose and PgBouncer; trace for a request appears in Jaeger |
