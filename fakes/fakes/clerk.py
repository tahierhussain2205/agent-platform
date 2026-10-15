"""Clerk stand-in.

Speaks the parts of Clerk the platform uses, with Clerk's shapes:
- Backend API under ``/clerk/v1`` (secret key bearer auth): users, organizations, memberships,
  invitations, JWKS.
- Session tokens: RS256 JWT v2 claims (``sub``, ``sid``, ``sts``, ``v``, ``o.{id,rol,slg}``,
  ``azp``, ``iss``, ``exp``, ``nbf``, ``iat``, ``jti``, ``fva``), JWKS at
  ``/clerk/.well-known/jwks.json``.
- Webhooks through the Svix deliverer: ``user.*``, ``organization.*``,
  ``organizationMembership.*``, ``organizationInvitation.*``.
- A dev JSON API under ``/clerk/dev`` that replaces ClerkJS for the console's dev auth adapter:
  sign up, sign in, session state, active org, tokens, create org, accept invitation, sign out.
"""

import hashlib
import json
import re
import secrets
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm

from fakes.config import Config
from fakes.store import Store
from fakes.svix import Deliverer

ROLE = {"admin": "org:admin", "user": "org:member"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 10_000).hex()


def clerk_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"errors": [{"code": code, "message": message, "long_message": message}]})


class Clerk:
    def __init__(self, config: Config, store: Store, deliverer: Deliverer) -> None:
        self.config = config
        self.store = store
        self.deliverer = deliverer

    # ------------------------------------------------------------------ state

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data.setdefault("clerk", {})
        return state

    def reset(self, fixture: dict[str, Any] | None, keep_key: dict[str, str] | None = None) -> None:
        """Back to the seed. The signing key survives resets (tests reset data, not keys)."""
        if keep_key is None:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            keep_key = {"kid": f"ins_local_{uuid.uuid4().hex[:8]}", "pem": pem.decode()}
        self.store.data["clerk"] = {
            "key": keep_key,
            "users": {}, "orgs": {}, "memberships": {}, "invitations": {}, "sessions": {},
        }  # fmt: skip
        if fixture:
            self._seed(fixture)
        self.store.save()

    def _seed(self, fixture: dict[str, Any]) -> None:
        clerk_ids = {u["email"]: u["clerk_user_id"] for u in fixture["users"]}
        for user in fixture["users"]:
            if user["clerk_user_id"]:
                first, _, last = user["name"].partition(" ")
                self._new_user(user["email"], self.config.dev_password, first, last, user_id=user["clerk_user_id"])
        for org in fixture["orgs"]:
            self.s["orgs"][org["clerk_org_id"]] = {
                "id": org["clerk_org_id"],
                "name": org["name"],
                "slug": org["key"],
                "created_by": None,
                "created_at": _now_ms(),
            }
            for member in org["members"]:
                if member["status"] == "active" and clerk_ids[member["email"]]:
                    mid = _id("orgmem")
                    self.s["memberships"][mid] = {
                        "id": mid,
                        "org_id": org["clerk_org_id"],
                        "user_id": clerk_ids[member["email"]],
                        "role": ROLE[member["privilege"]],
                        "created_at": _now_ms(),
                    }
                elif member["status"] == "invited":
                    iid = _id("orginv")
                    self.s["invitations"][iid] = {
                        "id": iid,
                        "org_id": org["clerk_org_id"],
                        "email": member["email"],
                        "role": ROLE[member["privilege"]],
                        "status": "pending",
                        "inviter_user_id": None,
                        "created_at": _now_ms(),
                    }

    def _new_user(self, email: str, password: str, first: str, last: str, user_id: str | None = None) -> dict[str, Any]:
        salt = secrets.token_hex(8)
        user = {
            "id": user_id or _id("user"),
            "email": email,
            "first_name": first,
            "last_name": last,
            "salt": salt,
            "password": _hash(password, salt),
            "created_at": _now_ms(),
        }
        self.s["users"][user["id"]] = user
        return user

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        return next((u for u in self.s["users"].values() if u["email"].lower() == email.lower()), None)

    def membership(self, org_id: str, user_id: str) -> dict[str, Any] | None:
        return next(
            (m for m in self.s["memberships"].values() if m["org_id"] == org_id and m["user_id"] == user_id), None
        )

    # ------------------------------------------------------------------ Clerk object shapes

    def user_obj(self, user: dict[str, Any]) -> dict[str, Any]:
        email_id = "idn_" + hashlib.sha1(user["email"].encode()).hexdigest()[:20]
        return {
            "id": user["id"], "object": "user", "first_name": user["first_name"], "last_name": user["last_name"],
            "primary_email_address_id": email_id,
            "email_addresses": [{"id": email_id, "object": "email_address", "email_address": user["email"], "verification": {"status": "verified"}}],
            "created_at": user["created_at"], "updated_at": user["created_at"],
        }  # fmt: skip

    def org_obj(self, org: dict[str, Any]) -> dict[str, Any]:
        members = sum(1 for m in self.s["memberships"].values() if m["org_id"] == org["id"])
        return {
            "id": org["id"],
            "object": "organization",
            "name": org["name"],
            "slug": org["slug"],
            "created_by": org["created_by"],
            "members_count": members,
            "created_at": org["created_at"],
            "updated_at": org["created_at"],
        }

    def membership_obj(self, m: dict[str, Any]) -> dict[str, Any]:
        user, org = self.s["users"][m["user_id"]], self.s["orgs"][m["org_id"]]
        return {
            "id": m["id"], "object": "organization_membership", "role": m["role"],
            "organization": self.org_obj(org),
            "public_user_data": {"user_id": user["id"], "identifier": user["email"], "first_name": user["first_name"], "last_name": user["last_name"]},
            "created_at": m["created_at"], "updated_at": m["created_at"],
        }  # fmt: skip

    def invitation_obj(self, inv: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": inv["id"],
            "object": "organization_invitation",
            "email_address": inv["email"],
            "role": inv["role"],
            "status": inv["status"],
            "organization_id": inv["org_id"],
            "created_at": inv["created_at"],
            "updated_at": inv["created_at"],
        }

    # ------------------------------------------------------------------ tokens

    def jwks(self) -> dict[str, Any]:
        key = serialization.load_pem_private_key(self.s["key"]["pem"].encode(), password=None)
        jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))  # type: ignore[arg-type]
        return {"keys": [{**jwk, "kid": self.s["key"]["kid"], "use": "sig", "alg": "RS256"}]}

    def session_status(self, session: dict[str, Any]) -> str:
        has_org = any(m["user_id"] == session["user_id"] for m in self.s["memberships"].values())
        return "active" if has_org else "pending"  # "organization required": pending until the user has one

    def token(self, session: dict[str, Any], origin: str) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "azp": origin, "exp": now + self.config.clerk_token_ttl, "fva": [0, -1], "iat": now, "iss": self.config.clerk_issuer,
            "jti": uuid.uuid4().hex[:20], "nbf": now - 2, "sid": session["id"], "sub": session["user_id"], "v": 2,
            "sts": self.session_status(session),
        }  # fmt: skip
        org_id = session.get("active_org_id")
        if org_id and (m := self.membership(org_id, session["user_id"])):
            claims["o"] = {
                "id": org_id,
                "rol": m["role"].removeprefix("org:"),
                "slg": self.s["orgs"][org_id]["slug"],
                "per": "",
                "fpm": "",
            }
        return jwt.encode(claims, self.s["key"]["pem"], algorithm="RS256", headers={"kid": self.s["key"]["kid"]})

    # ------------------------------------------------------------------ mutations (emit webhooks)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.deliverer.enqueue(event, data)

    def create_org(self, name: str, creator_id: str) -> dict[str, Any]:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"
        org: dict[str, Any] = {
            "id": _id("org"),
            "name": name,
            "slug": f"{slug}-{secrets.token_hex(2)}",
            "created_by": creator_id,
            "created_at": _now_ms(),
        }
        self.s["orgs"][org["id"]] = org
        self.emit("organization.created", self.org_obj(org))
        self.add_membership(org["id"], creator_id, "org:admin")
        return org

    def add_membership(self, org_id: str, user_id: str, role: str) -> dict[str, Any]:
        m = {"id": _id("orgmem"), "org_id": org_id, "user_id": user_id, "role": role, "created_at": _now_ms()}
        self.s["memberships"][m["id"]] = m
        self.emit("organizationMembership.created", self.membership_obj(m))
        return m


def build_router(clerk: Clerk) -> APIRouter:
    router = APIRouter(prefix="/clerk")
    cfg = clerk.config

    def backend_auth(authorization: str | None) -> None:
        if authorization != f"Bearer {cfg.clerk_secret_key}":
            raise clerk_error(401, "authentication_invalid", "Invalid secret key")

    def session_of(authorization: str | None) -> dict[str, Any]:
        sid = (authorization or "").removeprefix("Bearer ").strip()
        session = clerk.s["sessions"].get(sid)
        if not session or session["status"] != "active":
            raise clerk_error(401, "session_invalid", "Sign in again")
        return session  # type: ignore[no-any-return]

    def session_state(session: dict[str, Any]) -> dict[str, Any]:
        user = clerk.s["users"][session["user_id"]]
        orgs = [
            {"id": m["org_id"], "name": clerk.s["orgs"][m["org_id"]]["name"], "slug": clerk.s["orgs"][m["org_id"]]["slug"], "role": m["role"]}
            for m in clerk.s["memberships"].values() if m["user_id"] == user["id"]
        ]  # fmt: skip
        invitations = [
            {**clerk.invitation_obj(i), "organization_name": clerk.s["orgs"][i["org_id"]]["name"]}
            for i in clerk.s["invitations"].values() if i["email"].lower() == user["email"].lower() and i["status"] == "pending"
        ]  # fmt: skip
        if session.get("active_org_id") not in {o["id"] for o in orgs}:
            session["active_org_id"] = orgs[0]["id"] if orgs else None
        return {
            "session_id": session["id"], "status": clerk.session_status(session), "active_org_id": session["active_org_id"],
            "user": {"id": user["id"], "email": user["email"], "first_name": user["first_name"], "last_name": user["last_name"]},
            "organizations": sorted(orgs, key=lambda o: o["name"]), "invitations": invitations,
        }  # fmt: skip

    def new_session(user: dict[str, Any]) -> dict[str, Any]:
        session = {
            "id": "sess_" + secrets.token_urlsafe(24),
            "user_id": user["id"],
            "active_org_id": None,
            "status": "active",
            "created_at": _now_ms(),
        }
        clerk.s["sessions"][session["id"]] = session
        return session

    def origin_of(request: Request) -> str:
        origin = request.headers.get("origin") or cfg.console_origins[0]
        return origin.rstrip("/")

    # ------------------------------------------------------------------ JWKS

    @router.get("/.well-known/jwks.json")
    def jwks_public() -> dict[str, Any]:
        with clerk.store.lock:
            return clerk.jwks()

    # ------------------------------------------------------------------ dev auth API (replaces ClerkJS locally)

    @router.post("/dev/sign-up")
    def sign_up(body: dict[str, str]) -> dict[str, Any]:
        email, password = body.get("email", "").strip(), body.get("password", "")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(password) < 8:
            raise clerk_error(
                422, "form_param_format_invalid", "Enter a valid email and a password of at least 8 characters"
            )
        with clerk.store.lock:
            if clerk.user_by_email(email):
                raise clerk_error(422, "form_identifier_exists", "That email address is taken")
            user = clerk._new_user(
                email, password, body.get("first_name", "").strip(), body.get("last_name", "").strip()
            )
            clerk.emit("user.created", clerk.user_obj(user))
            session = new_session(user)
            state = session_state(session)
            clerk.store.save()
            return state

    @router.post("/dev/sign-in")
    def sign_in(body: dict[str, str]) -> dict[str, Any]:
        with clerk.store.lock:
            user = clerk.user_by_email(body.get("email", ""))
            if not user or _hash(body.get("password", ""), user["salt"]) != user["password"]:
                raise clerk_error(422, "form_password_incorrect", "Incorrect email or password")
            session = new_session(user)
            state = session_state(session)
            clerk.store.save()
            return state

    @router.get("/dev/session")
    def get_session(authorization: str | None = Header(None)) -> dict[str, Any]:
        with clerk.store.lock:
            return session_state(session_of(authorization))

    @router.post("/dev/session/active-org")
    def set_active_org(body: dict[str, str | None], authorization: str | None = Header(None)) -> dict[str, Any]:
        with clerk.store.lock:
            session = session_of(authorization)
            org_id = body.get("organization_id")
            if org_id and not clerk.membership(org_id, session["user_id"]):
                raise clerk_error(403, "not_a_member", "You are not a member of that organization")
            session["active_org_id"] = org_id
            clerk.store.save()
            return session_state(session)

    @router.post("/dev/session/tokens")
    def session_token(request: Request, authorization: str | None = Header(None)) -> dict[str, str]:
        with clerk.store.lock:
            session = session_of(authorization)
            session_state(session)  # normalizes the active org
            return {"jwt": clerk.token(session, origin_of(request))}

    @router.post("/dev/session/end")
    def end_session(authorization: str | None = Header(None)) -> dict[str, str]:
        with clerk.store.lock:
            session_of(authorization)["status"] = "ended"
            clerk.store.save()
            return {"status": "ended"}

    @router.post("/dev/organizations")
    def create_org(body: dict[str, str], authorization: str | None = Header(None)) -> dict[str, Any]:
        name = body.get("name", "").strip()
        if not 1 <= len(name) <= 200:
            raise clerk_error(422, "form_param_format_invalid", "Enter an organization name")
        with clerk.store.lock:
            session = session_of(authorization)
            org = clerk.create_org(name, session["user_id"])
            session["active_org_id"] = org["id"]
            clerk.store.save()
            return session_state(session)

    @router.post("/dev/invitations/{invitation_id}/accept")
    def accept_invitation(invitation_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        with clerk.store.lock:
            session = session_of(authorization)
            user = clerk.s["users"][session["user_id"]]
            inv = clerk.s["invitations"].get(invitation_id)
            if not inv or inv["status"] != "pending" or inv["email"].lower() != user["email"].lower():
                raise clerk_error(404, "resource_not_found", "Invitation not found")
            inv["status"] = "accepted"
            clerk.emit("organizationInvitation.accepted", clerk.invitation_obj(inv))
            if not clerk.membership(inv["org_id"], user["id"]):
                clerk.add_membership(inv["org_id"], user["id"], inv["role"])
            session["active_org_id"] = inv["org_id"]
            clerk.store.save()
            return session_state(session)

    # ------------------------------------------------------------------ Backend API

    @router.get("/v1/jwks")
    def jwks_backend(authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        with clerk.store.lock:
            return clerk.jwks()

    @router.get("/v1/users/{user_id}")
    def get_user(user_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        user = clerk.s["users"].get(user_id)
        if not user:
            raise clerk_error(404, "resource_not_found", "User not found")
        return clerk.user_obj(user)

    def org_or_404(org_id: str) -> dict[str, Any]:
        org = clerk.s["orgs"].get(org_id)
        if not org:
            raise clerk_error(404, "resource_not_found", "Organization not found")
        return org  # type: ignore[no-any-return]

    @router.get("/v1/organizations/{org_id}")
    def get_org(org_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        return clerk.org_obj(org_or_404(org_id))

    @router.patch("/v1/organizations/{org_id}")
    def update_org(org_id: str, body: dict[str, Any], authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        with clerk.store.lock:
            org = org_or_404(org_id)
            if "name" in body:
                if not str(body["name"]).strip():
                    raise clerk_error(422, "form_param_format_invalid", "Name is required")
                org["name"] = str(body["name"]).strip()
            clerk.emit("organization.updated", clerk.org_obj(org))
            clerk.store.save()
            return clerk.org_obj(org)

    @router.get("/v1/organizations/{org_id}/memberships")
    def list_memberships(org_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        org_or_404(org_id)
        data = [clerk.membership_obj(m) for m in clerk.s["memberships"].values() if m["org_id"] == org_id]
        return {"data": data, "total_count": len(data)}

    @router.patch("/v1/organizations/{org_id}/memberships/{user_id}")
    def update_membership(
        org_id: str, user_id: str, body: dict[str, Any], authorization: str | None = Header(None)
    ) -> dict[str, Any]:
        backend_auth(authorization)
        if body.get("role") not in ("org:admin", "org:member"):
            raise clerk_error(422, "form_param_value_invalid", "Unknown role")
        with clerk.store.lock:
            m = clerk.membership(org_id, user_id)
            if not m:
                raise clerk_error(404, "resource_not_found", "Membership not found")
            m["role"] = body["role"]
            clerk.emit("organizationMembership.updated", clerk.membership_obj(m))
            clerk.store.save()
            return clerk.membership_obj(m)

    @router.delete("/v1/organizations/{org_id}/memberships/{user_id}")
    def delete_membership(org_id: str, user_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        with clerk.store.lock:
            m = clerk.membership(org_id, user_id)
            if not m:
                raise clerk_error(404, "resource_not_found", "Membership not found")
            obj = clerk.membership_obj(m)
            del clerk.s["memberships"][m["id"]]
            clerk.emit("organizationMembership.deleted", obj)
            clerk.store.save()
            return obj

    @router.post("/v1/organizations/{org_id}/invitations")
    def create_invitation(
        org_id: str, body: dict[str, Any], authorization: str | None = Header(None)
    ) -> dict[str, Any]:
        backend_auth(authorization)
        email, role = str(body.get("email_address", "")).strip(), body.get("role")
        if role not in ("org:admin", "org:member") or "@" not in email:
            raise clerk_error(422, "form_param_value_invalid", "Invalid email or role")
        with clerk.store.lock:
            org_or_404(org_id)
            user = clerk.user_by_email(email)
            if user and clerk.membership(org_id, user["id"]):
                raise clerk_error(400, "already_a_member_in_organization", "Already a member")
            if any(
                i["org_id"] == org_id and i["email"].lower() == email.lower() and i["status"] == "pending"
                for i in clerk.s["invitations"].values()
            ):
                raise clerk_error(400, "duplicate_record", "There is already a pending invitation for this email")
            inv = {
                "id": _id("orginv"),
                "org_id": org_id,
                "email": email,
                "role": role,
                "status": "pending",
                "inviter_user_id": body.get("inviter_user_id"),
                "created_at": _now_ms(),
            }
            clerk.s["invitations"][inv["id"]] = inv
            clerk.emit("organizationInvitation.created", clerk.invitation_obj(inv))
            clerk.store.save()
            return clerk.invitation_obj(inv)

    @router.get("/v1/organizations/{org_id}/invitations")
    def list_invitations(
        org_id: str, status: str | None = None, authorization: str | None = Header(None)
    ) -> dict[str, Any]:
        backend_auth(authorization)
        org_or_404(org_id)
        data = [
            clerk.invitation_obj(i)
            for i in clerk.s["invitations"].values()
            if i["org_id"] == org_id and (status is None or i["status"] == status)
        ]
        return {"data": data, "total_count": len(data)}

    @router.post("/v1/organizations/{org_id}/invitations/{invitation_id}/revoke")
    def revoke_invitation(org_id: str, invitation_id: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        backend_auth(authorization)
        with clerk.store.lock:
            inv = clerk.s["invitations"].get(invitation_id)
            if not inv or inv["org_id"] != org_id:
                raise clerk_error(404, "resource_not_found", "Invitation not found")
            if inv["status"] != "pending":
                raise clerk_error(400, "invitation_not_pending", "Only pending invitations can be revoked")
            inv["status"] = "revoked"
            clerk.emit("organizationInvitation.revoked", clerk.invitation_obj(inv))
            clerk.store.save()
            return clerk.invitation_obj(inv)

    return router


async def clerk_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"errors": [{"code": "error", "message": str(exc.detail)}]}
    return JSONResponse(detail, status_code=exc.status_code)
