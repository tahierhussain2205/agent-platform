"""Shared pieces of the OAuth provider stand-ins: consent pages, codes and token records."""

import html
import secrets
import time
from typing import Any
from urllib.parse import urlencode

CODE_TTL = 600


def consent_page(title: str, action: str, hidden: dict[str, str], accounts: list[tuple[str, str]], scopes: str) -> str:
    """An Allow / Deny page where the person picks the account they authorize as."""
    fields = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in hidden.items()
    )
    options = "".join(
        f'<label><input type="radio" name="account" value="{html.escape(value)}"{" checked" if i == 0 else ""}> '
        f"{html.escape(label)}</label><br>"
        for i, (value, label) in enumerate(accounts)
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="font-family: system-ui; max-width: 560px; margin: 40px auto">
<h1>{html.escape(title)}</h1>
<p>Requested access: <code>{html.escape(scopes)}</code></p>
<form method="post" action="{html.escape(action)}">{fields}
<fieldset><legend>Authorize as</legend>{options}</fieldset>
<p><button name="decision" value="allow">Allow</button> <button name="decision" value="deny">Deny</button></p>
</form></body></html>"""


def redirect_to(uri: str, **params: str) -> str:
    return f"{uri}{'&' if '?' in uri else '?'}{urlencode({k: v for k, v in params.items() if v is not None})}"


def new_code(store: dict[str, Any], **data: Any) -> str:
    code = secrets.token_urlsafe(24)
    store[code] = {**data, "exp": time.time() + CODE_TTL}
    return code


def take_code(store: dict[str, Any], code: str) -> dict[str, Any] | None:
    """Codes are single use and expire."""
    data = store.pop(code, None)
    if data is None or data["exp"] < time.time():
        return None
    return data  # type: ignore[no-any-return]


def issue_tokens(state: dict[str, Any], prefix: str, ttl: int, **data: Any) -> dict[str, Any]:
    access = f"{prefix}a_{secrets.token_urlsafe(24)}"
    refresh = f"{prefix}r_{secrets.token_urlsafe(24)}"
    state.setdefault("access", {})[access] = {**data, "exp": time.time() + ttl}
    state.setdefault("refresh", {})[refresh] = dict(data)
    return {"access_token": access, "refresh_token": refresh, "expires_in": ttl}


def bearer(state: dict[str, Any], authorization: str | None) -> dict[str, Any] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    record = state.get("access", {}).get(authorization.split(" ", 1)[1].strip())
    if record is None or record["exp"] < time.time():
        return None
    return record  # type: ignore[no-any-return]


def expire_all(state: dict[str, Any]) -> int:
    for record in state.get("access", {}).values():
        record["exp"] = 0
    return len(state.get("access", {}))
