"""Database errors mapped to HTTP responses (spec 14.1).

The raw database message can contain row values, so clients get a generic message plus the
constraint name; the full error is logged.
"""

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class MappedError:
    status: int
    code: str
    message: str
    constraint: str | None = None


_BY_SQLSTATE: dict[str, tuple[int, str, str]] = {
    "42501": (403, "forbidden", "Not allowed."),  # insufficient privilege, RLS violation
    "23505": (409, "conflict", "This conflicts with an existing record."),  # unique violation
    "23503": (409, "conflict", "This refers to a record that does not exist."),  # foreign key
    "23514": (422, "invalid", "This violates a rule."),  # check violation (and our triggers)
    "23502": (422, "invalid", "A required value is missing."),  # not null
}


def map_db_error(exc: psycopg.Error) -> MappedError | None:
    mapped = _BY_SQLSTATE.get(exc.sqlstate or "")
    if mapped is None:
        return None
    status, code, message = mapped
    constraint = exc.diag.constraint_name if exc.diag else None
    return MappedError(status, code, message, constraint)


def error_body(
    code: str, message: str, request_id: str | None, constraint: str | None = None
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if constraint:
        error["constraint"] = constraint
    if request_id:
        error["request_id"] = request_id
    return {"error": error}
