"""Google Cloud stand-in: service account tokens, Cloud Logging, Error Reporting and Cloud Trace.

- ``POST /gcp/token``: the service account flow (``grant_type`` jwt-bearer): an RS256 assertion
  signed with the service account's key, verified against its public key (``GCP_SA_PUBLIC_KEY``),
  with ``iss`` the account, ``aud`` this endpoint, at most one hour; returns a one hour token.
- ``POST /gcp/logging/v2/entries:list``: ``resourceNames``, ``filter`` (the subset of the Logging
  query language the platform generates: ``AND`` of comparisons, parenthesized ``OR`` of ``:``
  matches), ``orderBy``, ``pageSize`` and ``pageToken``.
- ``GET /gcp/errorreporting/v1beta1/projects/{p}/groupStats``: groups of error entries for a
  ``timeRange.period`` and an optional ``serviceFilter.service``.
- ``GET /gcp/trace/v1/projects/{p}/traces/{id}``.

The account may read ``acme-prod`` only; ``globex-prod`` exists to be refused. Fixture logs follow
the GitHub and Jira fixtures (cart-service checkout errors, webhook signature failures after a key
rotation, slow EU searches) on top of routine noise.
"""

import hashlib
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fakes.config import Config
from fakes.store import Store, outdated

TIME = "%Y-%m-%dT%H:%M:%S.%fZ"
SEVERITIES = ["DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"]
READ_SCOPES = {
    "https://www.googleapis.com/auth/cloud-platform.read-only",
    "https://www.googleapis.com/auth/logging.read",
}
PERIODS = {"PERIOD_1_HOUR": 1, "PERIOD_6_HOURS": 6, "PERIOD_1_DAY": 24, "PERIOD_1_WEEK": 168, "PERIOD_30_DAYS": 720}
CHECKOUT_TRACE = """Traceback (most recent call last):
  File "/app/src/api/checkout.py", line 41, in checkout
    amount = total(cart.items)
  File "/app/src/pricing/cart.py", line 7, in total
    prices[n] = item.price
IndexError: list assignment index out of range"""
WEBHOOK_TRACE = """Traceback (most recent call last):
  File "/app/src/api/webhooks.py", line 18, in receive
    if not verify(body, signature):
  File "/app/src/webhooks/verify.py", line 9, in verify
    raise SignatureError(f"signature mismatch for event {event}")
SignatureError: signature mismatch for event evt_{n}"""


def _id(*parts: Any) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:32]


def seed(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    entries: list[dict[str, Any]] = []
    traces: dict[str, dict[str, Any]] = {}

    def resource(service: str, project: str = "acme-prod") -> dict[str, Any]:
        return {"type": "cloud_run_revision",
                "labels": {"project_id": project, "service_name": service, "location": "europe-west1"}}  # fmt: skip

    def add(project: str, service: str, at: datetime, severity: str, payload: dict[str, Any],
            trace: str | None = None, status: int | None = None) -> None:  # fmt: skip
        entry: dict[str, Any] = {
            "logName": f"projects/{project}/logs/run.googleapis.com%2Fstdout",
            "resource": resource(service, project), "timestamp": at.strftime(TIME), "severity": severity,
            "insertId": _id(project, service, at.isoformat(), payload.get("message")), "jsonPayload": payload,
            "labels": {"instanceId": "00a1"},
        }  # fmt: skip
        if trace:
            entry["trace"] = f"projects/{project}/traces/{trace}"
        if status:
            entry["httpRequest"] = {"requestMethod": "POST", "requestUrl": "/checkout", "status": status}
        entries.append(entry)

    # Routine traffic over three days.
    for minutes in range(0, 72 * 60, 20):
        at = now - timedelta(minutes=minutes)
        add("acme-prod", "cart-service", at, "INFO", {"message": "POST /checkout 200"}, status=200)
        add("acme-prod", "web-app", at, "INFO", {"message": "GET /search 200", "region": "us-central1"})
        if minutes % 180 == 0:
            add("acme-prod", "cart-service", at, "WARNING", {"message": "slow upstream: tax-service took 1200ms"})
    # PAY-101: carts of 20 or more items fail since the deploy 6.5 hours ago.
    for n in range(37):
        at = now - timedelta(hours=6.4) + timedelta(minutes=10 * n)
        trace = _id("checkout", n)
        add("acme-prod", "cart-service", at, "ERROR",
            {"message": "Unhandled exception in POST /checkout", "exception": CHECKOUT_TRACE,
             "cart_size": 20 + n % 12, "cart_id": f"c-{4100 + n}"}, trace=trace, status=500)  # fmt: skip
        traces[trace] = {"projectId": "acme-prod", "traceId": trace, "spans": [
            {"spanId": "1", "kind": "RPC_SERVER", "name": "POST /checkout", "startTime": at.strftime(TIME),
             "endTime": (at + timedelta(milliseconds=180)).strftime(TIME),
             "labels": {"service": "cart-service", "/http/status_code": "500"}},
            {"spanId": "2", "parentSpanId": "1", "kind": "INTERNAL", "name": "pricing.total",
             "startTime": (at + timedelta(milliseconds=20)).strftime(TIME),
             "endTime": (at + timedelta(milliseconds=25)).strftime(TIME),
             "labels": {"service": "cart-service", "error": "IndexError"}},
            {"spanId": "3", "parentSpanId": "1", "kind": "RPC_CLIENT", "name": "GET tax-service /rates",
             "startTime": (at + timedelta(milliseconds=5)).strftime(TIME),
             "endTime": (at + timedelta(milliseconds=18)).strftime(TIME), "labels": {"service": "tax-service"}},
        ]}  # fmt: skip
    # PAY-105: webhook signatures rejected from the key rotation (48 h ago) to the fix deploy (35 h ago).
    for n in range(26):
        at = now - timedelta(hours=48) + timedelta(minutes=30 * n)
        add("acme-prod", "cart-service", at, "ERROR",
            {"message": "Webhook signature verification failed", "exception": WEBHOOK_TRACE.replace("{n}", str(7000 + n)),
             "provider": "stripe"})  # fmt: skip
    # WEB-7: slow EU searches since the products_v2 deploy (29 h ago).
    for n in range(58):
        at = now - timedelta(hours=28.5) + timedelta(minutes=29 * n)
        add("acme-prod", "web-app", at, "WARNING",
            {"message": f"slow search request: {4100 + 37 * n % 2000}ms", "region": "europe-west1",
             "index": "products_v2", "latency_ms": 4100 + 37 * n % 2000})  # fmt: skip
    # Another org's project, which the account cannot read.
    add("globex-prod", "payments", now - timedelta(hours=1), "ERROR", {"message": "Globex secret incident"})
    entries.sort(key=lambda e: e["timestamp"])
    return {
        "version": 1,
        "entries": entries,
        "traces": traces,
        "projects": {"acme-prod": ["agent-reader@acme-prod.iam.gserviceaccount.com"], "globex-prod": []},
        "tokens": {},
    }


def error(status: int, message: str, reason: str) -> JSONResponse:
    return JSONResponse({"error": {"code": status, "message": message, "status": reason}}, status_code=status)


class FilterError(ValueError):
    pass


COMPARISON = re.compile(r'^([\w.]+)\s*(>=|<=|!=|=|>|<|:)\s*"((?:[^"\\]|\\.)*)"$|^(severity)\s*(>=|<=|=|>|<)\s*(\w+)$')


def _field(entry: dict[str, Any], path: str) -> Any:
    value: Any = entry
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _compare(entry: dict[str, Any], clause: str) -> bool:
    m = COMPARISON.match(clause.strip())
    if not m:
        raise FilterError(f"Unparseable filter: {clause.strip()}")
    if m.group(4):  # severity>=ERROR
        op, wanted = m.group(5), m.group(6).upper()
        if wanted not in SEVERITIES:
            raise FilterError(f"Unknown severity: {wanted}")
        have, ref = SEVERITIES.index(entry.get("severity", "DEFAULT")), SEVERITIES.index(wanted)
        return {">=": have >= ref, "<=": have <= ref, "=": have == ref, ">": have > ref, "<": have < ref}[op]
    path, op, raw = m.group(1), m.group(2), m.group(3).replace('\\"', '"')
    value = _field(entry, path)
    if op == ":":
        return isinstance(value, str) and raw.lower() in value.lower()
    if path == "timestamp":
        a, b = str(value), raw if "." in raw else raw.replace("Z", ".000000Z")
        return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "=": a == b, "!=": a != b}[op]
    return {"=": str(value) == raw, "!=": str(value) != raw}.get(op, False)


def matches(entry: dict[str, Any], filter_text: str) -> bool:
    for clause in re.split(r"\s+AND\s+", filter_text.strip()) if filter_text.strip() else []:
        clause = clause.strip()
        if clause.startswith("(") and clause.endswith(")"):
            if not any(_compare(entry, alt) for alt in re.split(r"\s+OR\s+", clause[1:-1])):
                return False
        elif not _compare(entry, clause):
            return False
    return True


def signature_of(entry: dict[str, Any]) -> str:
    payload = entry.get("jsonPayload") or {}
    last = str(payload.get("exception") or payload.get("message") or "").strip().splitlines()[-1:] or [""]
    return re.sub(r"\d+", "N", last[0])


class Gcp:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data["gcp"]
        return state

    def outdated(self) -> bool:
        return outdated(self.store.data.get("gcp") or {}, seed())

    def reset(self) -> None:
        self.store.data["gcp"] = seed()

    def caller(self, request: Request) -> dict[str, Any] | None:
        auth = request.headers.get("authorization", "")
        record = self.s["tokens"].get(auth[7:]) if auth.startswith("Bearer ") else None
        return record if record and record["exp"] > time.time() else None

    def readable(self, record: dict[str, Any], project: str) -> bool:
        return record["account"] in self.s["projects"].get(project, [])


def build_router(gcp: Gcp) -> APIRouter:
    router = APIRouter()
    cfg = gcp.config

    @router.post("/gcp/token")
    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        if not cfg.gcp_sa_public_key:
            return JSONResponse({"error": "invalid_grant", "error_description": "no keys"}, status_code=400)
        try:
            claims = jwt.decode(
                str(form.get("assertion") or ""), cfg.gcp_sa_public_key, algorithms=["RS256"],
                options={"require": ["iss", "aud", "iat", "exp"], "verify_aud": False},
                leeway=60,  # Google tolerates small clock differences
            )  # fmt: skip
        except jwt.PyJWTError as exc:
            return JSONResponse({"error": "invalid_grant", "error_description": f"Invalid JWT: {exc}"}, status_code=400)
        if not str(claims["aud"]).endswith("/token") or claims["exp"] - claims["iat"] > 3600:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid JWT claims"}, status_code=400)
        if claims["iss"] != cfg.gcp_sa_email:
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid issuer"}, status_code=400)
        scopes = set(str(claims.get("scope", "")).split())
        if not scopes & READ_SCOPES:
            return JSONResponse({"error": "invalid_scope"}, status_code=400)
        access = "ya29.local-" + secrets.token_urlsafe(24)
        with gcp.store.lock:
            gcp.s["tokens"][access] = {"account": claims["iss"], "scopes": sorted(scopes), "exp": time.time() + 3600}
            gcp.store.save()
        return JSONResponse({"access_token": access, "expires_in": 3599, "token_type": "Bearer"})

    @router.post("/gcp/logging/v2/entries:list")
    async def entries(request: Request) -> JSONResponse:
        record = gcp.caller(request)
        if record is None:
            return error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED")
        body = await request.json()
        projects = [str(r).removeprefix("projects/") for r in body.get("resourceNames") or []]
        if not projects:
            return error(400, "resourceNames is required", "INVALID_ARGUMENT")
        for project in projects:
            if not gcp.readable(record, project):
                return error(403, f"Permission denied for project {project}", "PERMISSION_DENIED")
        try:
            found = [
                e for e in gcp.s["entries"]
                if e["resource"]["labels"]["project_id"] in projects and matches(e, str(body.get("filter") or ""))
            ]  # fmt: skip
        except FilterError as exc:
            return error(400, str(exc), "INVALID_ARGUMENT")
        if str(body.get("orderBy") or "timestamp asc").endswith("desc"):
            found.reverse()
        size = max(1, min(int(body.get("pageSize") or 50), 1000))
        start = int(body.get("pageToken") or 0)
        page = found[start : start + size]
        result: dict[str, Any] = {"entries": page}
        if start + size < len(found):
            result["nextPageToken"] = str(start + size)
        return JSONResponse(result)

    @router.get("/gcp/errorreporting/v1beta1/projects/{project}/groupStats")
    async def group_stats(request: Request, project: str) -> JSONResponse:
        record = gcp.caller(request)
        if record is None:
            return error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED")
        if not gcp.readable(record, project):
            return error(403, f"Permission denied for project {project}", "PERMISSION_DENIED")
        q = request.query_params
        period = PERIODS.get(q.get("timeRange.period", "PERIOD_1_DAY"))
        if period is None:
            return error(400, "Unknown timeRange.period", "INVALID_ARGUMENT")
        since = (datetime.now(UTC) - timedelta(hours=period)).strftime(TIME)
        groups: dict[str, dict[str, Any]] = {}
        for e in gcp.s["entries"]:
            service = e["resource"]["labels"]["service_name"]
            if e["resource"]["labels"]["project_id"] != project or e["timestamp"] < since:
                continue
            if SEVERITIES.index(e["severity"]) < SEVERITIES.index("ERROR"):
                continue
            if q.get("serviceFilter.service") and service != q["serviceFilter.service"]:
                continue
            sig = signature_of(e)
            group_id = _id(project, service, sig)[:16]
            g = groups.setdefault(group_id, {
                "group": {"name": f"projects/{project}/groups/{group_id}", "groupId": group_id},
                "count": "0", "firstSeenTime": e["timestamp"], "lastSeenTime": e["timestamp"],
                "affectedServices": [{"service": service}],
                "representative": {"message": (e["jsonPayload"].get("exception") or e["jsonPayload"]["message"]),
                                   "serviceContext": {"service": service}, "eventTime": e["timestamp"]},
            })  # fmt: skip
            g["count"] = str(int(g["count"]) + 1)
            g["lastSeenTime"] = max(g["lastSeenTime"], e["timestamp"])
        ordered = sorted(groups.values(), key=lambda g: -int(g["count"]))
        return JSONResponse({"errorGroupStats": ordered})

    @router.get("/gcp/trace/v1/projects/{project}/traces/{trace_id}")
    async def trace(request: Request, project: str, trace_id: str) -> JSONResponse:
        record = gcp.caller(request)
        if record is None:
            return error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED")
        if not gcp.readable(record, project):
            return error(403, f"Permission denied for project {project}", "PERMISSION_DENIED")
        found = gcp.s["traces"].get(trace_id)
        if found is None or found["projectId"] != project:
            return error(404, "Trace not found", "NOT_FOUND")
        return JSONResponse(found)

    @router.post("/_admin/gcp/expire-tokens")
    def expire() -> dict[str, int]:
        with gcp.store.lock:
            for record in gcp.s["tokens"].values():
                record["exp"] = 0
            gcp.store.save()
        return {"expired": len(gcp.s["tokens"])}

    return router
