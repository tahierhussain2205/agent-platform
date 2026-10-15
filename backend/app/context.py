"""Per request (or per task) context, carried by contextvars so it follows async code."""

from contextvars import ContextVar
from uuid import UUID

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
org_id_var: ContextVar[UUID | None] = ContextVar("org_id", default=None)
