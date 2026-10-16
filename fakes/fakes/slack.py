"""Slack stand-in.

- Web API under ``/slack/api/<method>``: bot token auth, Slack's ``{"ok": ...}`` envelope and error
  codes, cursor pagination, optional per method rate limits (429 + Retry-After).
- OAuth v2: ``/slack/oauth/v2/authorize`` (an Allow page) and ``oauth.v2.access``.
- Events API and interactivity: signed (``v0`` HMAC) deliveries to the configured URLs, with
  Slack style retries (``X-Slack-Retry-Num``).
- A small Slack-like UI (``/slack/ui``) and its JSON endpoints, plus admin endpoints for tests.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from fakes.config import Config
from fakes.store import Store

log = logging.getLogger("fakes.slack")
HISTORY = Path(__file__).with_name("data") / "slack_history.json"
UI = Path(__file__).with_name("slack_ui.html")
RETRY_DELAYS = (0.2, 1.0, 3.0)
APP_ID = "A0AGENT"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()


def new_ts() -> str:
    # Unique, increasing message timestamps like Slack's "1700000000.000100".
    now = time.time()
    return f"{now:.6f}"


@dataclass
class SlackDelivery:
    kind: str  # event | interaction
    url: str
    body: bytes
    content_type: str
    summary: dict[str, Any]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    delivered: bool = False


class SlackDeliverer:
    def __init__(self, secret: str, retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.secret = secret
        self.retry_delays = retry_delays
        self.queue: asyncio.Queue[SlackDelivery] = asyncio.Queue()
        self.log: list[SlackDelivery] = []
        self._task: asyncio.Task[None] | None = None
        self._http = httpx.AsyncClient(timeout=3.0)  # Slack gives up after about 3 seconds

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._http.aclose()

    def enqueue(self, delivery: SlackDelivery) -> None:
        self.log.append(delivery)
        self.queue.put_nowait(delivery)

    async def drain(self, limit_seconds: float = 30.0) -> None:
        async with asyncio.timeout(limit_seconds):
            await self.queue.join()

    async def _send(self, delivery: SlackDelivery, retry: int) -> bool:
        ts = int(time.time())
        headers = {
            "content-type": delivery.content_type,
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": sign(self.secret, ts, delivery.body),
        }
        if retry:
            headers.update({"x-slack-retry-num": str(retry), "x-slack-retry-reason": "http_error"})
        try:
            response = await self._http.post(delivery.url, content=delivery.body, headers=headers)
            delivery.attempts.append({"status": response.status_code})
            return response.status_code == 200
        except httpx.HTTPError as exc:
            delivery.attempts.append({"error": type(exc).__name__})
            return False

    async def _run(self) -> None:
        while True:
            delivery = await self.queue.get()
            try:
                for retry, delay in enumerate((0.0, *self.retry_delays)):
                    if delay:
                        await asyncio.sleep(delay)
                    if await self._send(delivery, retry):
                        delivery.delivered = True
                        break
            finally:
                self.queue.task_done()


class SlackApiError(Exception):
    def __init__(self, error: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.status = status
        self.headers = headers or {}


class Slack:
    def __init__(self, config: Config, store: Store, deliverer: SlackDeliverer) -> None:
        self.config = config
        self.store = store
        self.deliverer = deliverer
        self._rate: dict[tuple[str, str, int], int] = {}

    @property
    def s(self) -> dict[str, Any]:
        state: dict[str, Any] = self.store.data.setdefault("slack", {})
        return state

    # ------------------------------------------------------------------ seed

    def reset(self, fixture: dict[str, Any] | None) -> None:
        self.store.data["slack"] = {"teams": {}, "tokens": {}, "codes": {}, "rate_limits": {}}
        if fixture:
            self._seed(fixture)
        self._seed_team(
            "T0NEWCO",
            "Newco",
            "U0NEWCOBOT",
            installed=False,
            people=[("U0NIA", "nia@newco.test", "Nia Newco")],
            channels=[("C0NCGENERAL", "general", False, ["U0NIA"])],
        )
        self.store.save()

    def _seed_team(
        self,
        team_id: str,
        name: str,
        bot: str,
        installed: bool,
        people: list[tuple[str, str, str]],
        channels: list[tuple[str, str, bool, list[str]]],
    ) -> dict[str, Any]:
        team: dict[str, Any] = {
            "id": team_id,
            "name": name,
            "domain": name.lower().replace(" ", ""),
            "bot_user_id": bot,
            "installed": installed,
            "users": {},
            "channels": {},
            "ims": {},
            "ephemeral": [],
            "home": {},
        }
        team["users"][bot] = {
            "id": bot,
            "name": "agent",
            "real_name": "Agent",
            "is_bot": True,
            "profile": {"real_name": "Agent", "display_name": "agent"},
        }
        for user_id, email, real_name in people:
            team["users"][user_id] = self._user(user_id, email, real_name)
        for channel_id, channel_name, private, members in channels:
            team["channels"][channel_id] = {
                "id": channel_id,
                "name": channel_name,
                "is_private": private,
                "is_archived": False,
                "members": list(members),
                "messages": [],
            }
        self.s["teams"][team_id] = team
        if installed:
            self.s["tokens"][f"xoxb-local-{team_id}"] = {"team": team_id}
        return team

    @staticmethod
    def _user(user_id: str, email: str, real_name: str) -> dict[str, Any]:
        return {
            "id": user_id,
            "name": email.split("@")[0],
            "real_name": real_name,
            "is_bot": False,
            "deleted": False,
            "profile": {"email": email, "real_name": real_name, "display_name": real_name.split(" ")[0]},
        }

    def _seed(self, fixture: dict[str, Any]) -> None:
        names = {u["email"]: u["name"] for u in fixture["users"]}
        history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
        for org in fixture["orgs"]:
            ws = org["workspace"]
            people = [(m["slack_user_id"], m["email"], names[m["email"]]) for m in org["members"] if m["slack_user_id"]]
            people += [(u["slack_user_id"], u["email"], u["name"]) for u in ws.get("slack_only_users", [])]
            channels = [
                (
                    c["external_id"],
                    c["name"],
                    c["is_private"],
                    [*c["members"], *([ws["bot_user_id"]] if c["bot_is_member"] else [])],
                )
                for c in org["channels"]
            ]
            team = self._seed_team(
                ws["external_id"], ws["name"], ws["bot_user_id"], installed=True, people=people, channels=channels
            )
            for c in org["channels"]:
                team["channels"][c["external_id"]]["is_archived"] = c["archived"]
            now = time.time()
            for channel_id, messages in history.get(ws["external_id"], {}).items():
                channel = team["channels"].get(channel_id)
                if not channel:
                    continue
                posted: list[dict[str, Any]] = []
                for m in messages:
                    ts = f"{now - m['minutes_ago'] * 60:.6f}"
                    message = {"ts": ts, "user": m["user"], "text": m["text"]}
                    if "reply_to" in m:
                        message["thread_ts"] = posted[m["reply_to"]]["ts"]
                    posted.append(message)
                channel["messages"] = sorted(posted, key=lambda x: float(x["ts"]))

    # ------------------------------------------------------------------ helpers

    def team(self, team_id: str) -> dict[str, Any]:
        team = self.s["teams"].get(team_id)
        if not team:
            raise SlackApiError("team_not_found")
        return team  # type: ignore[no-any-return]

    def team_for_token(self, token: str | None) -> dict[str, Any]:
        info = self.s["tokens"].get(token or "")
        if not info:
            raise SlackApiError("invalid_auth")
        team = self.team(info["team"])
        if not team["installed"]:
            raise SlackApiError("account_inactive")
        return team

    def channel(self, team: dict[str, Any], channel_id: str) -> dict[str, Any]:
        if channel_id in team["channels"]:
            return team["channels"][channel_id]  # type: ignore[no-any-return]
        for user_id, im in team["ims"].items():
            if im["id"] == channel_id:
                return {**im, "user": user_id}
        raise SlackApiError("channel_not_found")

    def im_for(self, team: dict[str, Any], user_id: str) -> dict[str, Any]:
        if user_id not in team["users"]:
            raise SlackApiError("user_not_found")
        if user_id not in team["ims"]:
            team["ims"][user_id] = {"id": "D" + user_id[1:], "is_im": True, "messages": []}
        return team["ims"][user_id]  # type: ignore[no-any-return]

    def channel_obj(self, team: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
        bot = team["bot_user_id"]
        return {
            "id": c["id"],
            "name": c["name"],
            "is_channel": not c["is_private"],
            "is_group": c["is_private"],
            "is_private": c["is_private"],
            "is_archived": c["is_archived"],
            "is_member": bot in c["members"],
            "num_members": len(c["members"]),
        }

    def check_rate(self, team_id: str, method: str) -> None:
        limit = self.s["rate_limits"].get(method)
        if not limit:
            return
        window = int(time.time())
        key = (team_id, method, window)
        self._rate[key] = self._rate.get(key, 0) + 1
        if self._rate[key] > limit:
            raise SlackApiError("ratelimited", 429, {"Retry-After": "1"})

    # ------------------------------------------------------------------ events and interactions

    def emit_event(self, team: dict[str, Any], event: dict[str, Any]) -> None:
        if not team["installed"] or not self.config.slack_events_url:
            return
        event_id = "Ev" + uuid.uuid4().hex[:10].upper()
        payload = {
            "token": "local-verification-token", "team_id": team["id"], "api_app_id": APP_ID, "event": {**event, "event_ts": event.get("event_ts", new_ts()), "team": team["id"]},
            "type": "event_callback", "event_id": event_id, "event_time": int(time.time()),
            "authorizations": [{"team_id": team["id"], "user_id": team["bot_user_id"], "is_bot": True}],
        }  # fmt: skip
        self.deliverer.enqueue(
            SlackDelivery(
                "event",
                self.config.slack_events_url,
                json.dumps(payload).encode(),
                "application/json",
                {"event_id": event_id, "type": event["type"], "team": team["id"]},
            )
        )

    def emit_interaction(self, team: dict[str, Any], payload: dict[str, Any]) -> None:
        if not team["installed"] or not self.config.slack_interactions_url:
            return
        body = urlencode({"payload": json.dumps(payload)}).encode()
        self.deliverer.enqueue(
            SlackDelivery(
                "interaction",
                self.config.slack_interactions_url,
                body,
                "application/x-www-form-urlencoded",
                {"type": payload["type"], "action": payload["actions"][0].get("action_id")},
            )
        )

    def user_post(
        self, team_id: str, user_id: str, channel_id: str, text: str, thread_ts: str | None = None
    ) -> dict[str, Any]:
        """A person posts: store it, then emit the events Slack would send the app."""
        team = self.team(team_id)
        if user_id not in team["users"]:
            raise SlackApiError("user_not_found")
        bot = team["bot_user_id"]
        if channel_id.startswith("D"):
            im = next((im for uid, im in team["ims"].items() if im["id"] == channel_id and uid == user_id), None)
            if im is None:
                raise SlackApiError("channel_not_found")
            message = {"ts": new_ts(), "user": user_id, "text": text, **({"thread_ts": thread_ts} if thread_ts else {})}
            im["messages"].append(message)
            self.emit_event(
                team,
                {
                    "type": "message",
                    "channel_type": "im",
                    "channel": channel_id,
                    "user": user_id,
                    "text": text,
                    "ts": message["ts"],
                    **({"thread_ts": thread_ts} if thread_ts else {}),
                },
            )
            return message
        channel = self.channel(team, channel_id)
        if user_id not in channel["members"]:
            raise SlackApiError("not_in_channel")
        if channel["is_archived"]:
            raise SlackApiError("is_archived")
        message = {"ts": new_ts(), "user": user_id, "text": text, **({"thread_ts": thread_ts} if thread_ts else {})}
        channel["messages"].append(message)
        if f"<@{bot}>" in text and bot in channel["members"]:
            self.emit_event(
                team,
                {
                    "type": "app_mention",
                    "user": user_id,
                    "text": text,
                    "ts": message["ts"],
                    "channel": channel_id,
                    **({"thread_ts": thread_ts} if thread_ts else {}),
                },
            )
        return message

    def join(self, team_id: str, channel_id: str, user_id: str) -> None:
        team = self.team(team_id)
        channel = self.channel(team, channel_id)
        if user_id in channel["members"]:
            return
        channel["members"].append(user_id)
        if team["bot_user_id"] in channel["members"]:
            self.emit_event(
                team,
                {
                    "type": "member_joined_channel",
                    "user": user_id,
                    "channel": channel_id,
                    "channel_type": "G" if channel["is_private"] else "C",
                },
            )

    def leave(self, team_id: str, channel_id: str, user_id: str) -> None:
        team = self.team(team_id)
        channel = self.channel(team, channel_id)
        if user_id not in channel["members"]:
            return
        bot_was_member = team["bot_user_id"] in channel["members"]
        channel["members"].remove(user_id)
        if bot_was_member:
            self.emit_event(
                team,
                {
                    "type": "member_left_channel",
                    "user": user_id,
                    "channel": channel_id,
                    "channel_type": "G" if channel["is_private"] else "C",
                },
            )

    # ------------------------------------------------------------------ Web API

    def api(
        self, method: str, token: str | None, params: dict[str, Any], basic: tuple[str, str] | None
    ) -> dict[str, Any]:
        if method == "oauth.v2.access":
            return self.oauth_access(params, basic)
        team = self.team_for_token(token)
        self.check_rate(team["id"], method)
        bot = team["bot_user_id"]
        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            raise SlackApiError("unknown_method")
        return {"ok": True, **handler(team, bot, params, token)}

    def oauth_access(self, params: dict[str, Any], basic: tuple[str, str] | None) -> dict[str, Any]:
        client_id, client_secret = basic or (params.get("client_id"), params.get("client_secret"))
        if client_id != self.config.slack_client_id or client_secret != self.config.slack_client_secret:
            raise SlackApiError("invalid_client")
        grant = self.s["codes"].pop(params.get("code", ""), None)
        if not grant or grant["expires"] < time.time():
            raise SlackApiError("invalid_code")
        if grant["redirect_uri"] != params.get("redirect_uri"):
            raise SlackApiError("bad_redirect_uri")
        team = self.team(grant["team"])
        token = f"xoxb-{team['id']}-{secrets.token_hex(8)}"
        self.s["tokens"][token] = {"team": team["id"]}
        team["installed"] = True
        return {
            "ok": True,
            "app_id": APP_ID,
            "authed_user": {"id": grant["user"]},
            "scope": grant["scope"],
            "token_type": "bot",
            "access_token": token,
            "bot_user_id": team["bot_user_id"],
            "team": {"id": team["id"], "name": team["name"]},
            "enterprise": None,
            "is_enterprise_install": False,
        }

    def m_auth_test(self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None) -> dict[str, Any]:
        return {
            "url": f"https://{team['domain']}.slack.test/",
            "team": team["name"],
            "user": "agent",
            "team_id": team["id"],
            "user_id": bot,
            "bot_id": "B" + bot[1:],
        }

    def m_auth_revoke(self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None) -> dict[str, Any]:
        self.s["tokens"].pop(token or "", None)
        return {"revoked": True}

    def _page(self, items: list[Any], p: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        start = int(p.get("cursor") or 0)
        limit = max(1, min(int(p.get("limit") or 100), 1000))
        page = items[start : start + limit]
        nxt = str(start + limit) if start + limit < len(items) else ""
        return page, {"response_metadata": {"next_cursor": nxt}}

    def m_conversations_list(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        types = set(str(p.get("types") or "public_channel").split(","))
        exclude_archived = str(p.get("exclude_archived", "false")).lower() == "true"
        items = []
        for c in team["channels"].values():
            if c["is_private"] and ("private_channel" not in types or bot not in c["members"]):
                continue
            if not c["is_private"] and "public_channel" not in types:
                continue
            if exclude_archived and c["is_archived"]:
                continue
            items.append(self.channel_obj(team, c))
        page, meta = self._page(items, p)
        return {"channels": page, **meta}

    def m_conversations_open(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        """The bot's DM with one person (Slack's ``users`` is a comma separated list; one is enough here)."""
        users = [u for u in str(p.get("users") or "").split(",") if u]
        if len(users) != 1:
            raise SlackApiError("not_enough_users" if not users else "too_many_users")
        im = self.im_for(team, users[0])
        return {"channel": {"id": im["id"], "is_im": True, "user": users[0]}}

    def m_conversations_info(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        c = self.channel(team, p.get("channel", ""))
        if c.get("is_im"):
            return {"channel": {"id": c["id"], "is_im": True, "user": c["user"]}}
        if c["is_private"] and bot not in c["members"]:
            raise SlackApiError("channel_not_found")
        return {"channel": self.channel_obj(team, c)}

    def thread_of(self, team: dict[str, Any], channel_id: str | None, ts: str | None) -> dict[str, Any]:
        """``thread_ts`` of a message, as Slack puts it in an interaction's container."""
        if not channel_id or not ts:
            return {}
        try:
            channel = self.channel(team, channel_id)
        except SlackApiError:
            return {}
        message = next((m for m in channel["messages"] if m["ts"] == ts), None)
        return {"thread_ts": message["thread_ts"]} if message and message.get("thread_ts") else {}

    def m_conversations_members(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        c = self.channel(team, p.get("channel", ""))
        if c.get("is_im") or (c["is_private"] and bot not in c["members"]):
            raise SlackApiError("channel_not_found")
        page, meta = self._page(list(c["members"]), p)  # like Slack: the bot is listed too
        return {"members": page, **meta}

    def _readable(self, team: dict[str, Any], bot: str, channel_id: str) -> dict[str, Any]:
        c = self.channel(team, channel_id)
        if not c.get("is_im") and bot not in c["members"]:
            raise SlackApiError("not_in_channel")
        return c

    def m_conversations_history(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        c = self._readable(team, bot, p.get("channel", ""))
        oldest, latest = float(p.get("oldest") or 0), float(p.get("latest") or 1e12)
        top = [
            m
            for m in c["messages"]
            if (not m.get("thread_ts") or m["thread_ts"] == m["ts"]) and oldest <= float(m["ts"]) <= latest
        ]
        newest_first = sorted(
            (self._visible(self._with_replies(c, m), p) for m in top), key=lambda m: float(m["ts"]), reverse=True
        )
        page, meta = self._page(newest_first, p)
        return {"messages": page, "has_more": bool(meta["response_metadata"]["next_cursor"]), **meta}

    @staticmethod
    def _with_replies(c: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
        count = sum(1 for r in c["messages"] if r.get("thread_ts") == m["ts"] and r["ts"] != m["ts"])
        return {**m, "type": "message", **({"reply_count": count, "thread_ts": m["ts"]} if count else {})}

    def m_conversations_replies(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        c = self._readable(team, bot, p.get("channel", ""))
        ts = p.get("ts", "")
        parent = next((m for m in c["messages"] if m["ts"] == ts), None)
        if parent is None:
            raise SlackApiError("thread_not_found")
        replies = [r for r in c["messages"] if r.get("thread_ts") == ts and r["ts"] != ts]
        items = [
            self._visible(self._with_replies(c, parent), p),
            *(self._visible({**r, "type": "message"}, p) for r in sorted(replies, key=lambda r: float(r["ts"]))),
        ]
        page, meta = self._page(items, p)
        return {"messages": page, "has_more": bool(meta["response_metadata"]["next_cursor"]), **meta}

    def _target(self, team: dict[str, Any], bot: str, channel_id: str) -> dict[str, Any]:
        if channel_id.startswith("U"):
            return self.im_for(team, channel_id)
        c = self.channel(team, channel_id)
        if c.get("is_im"):
            return team["ims"][c["user"]]  # type: ignore[no-any-return]
        if bot not in c["members"]:
            raise SlackApiError("not_in_channel")
        if c["is_archived"]:
            raise SlackApiError("is_archived")
        return c

    @staticmethod
    def _blocks(p: dict[str, Any]) -> list[Any] | None:
        raw = p.get("blocks")
        if raw is None:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw  # type: ignore[no-any-return]

    def m_chat_postMessage(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        if not p.get("text") and not p.get("blocks"):
            raise SlackApiError("no_text")
        target = self._target(team, bot, p.get("channel", ""))
        message = {
            "ts": new_ts(),
            "user": bot,
            "bot_id": "B" + bot[1:],
            "text": p.get("text", ""),
            "blocks": self._blocks(p),
            **({"thread_ts": p["thread_ts"]} if p.get("thread_ts") else {}),
            **({"metadata": self._metadata(p)} if p.get("metadata") else {}),
        }
        target["messages"].append(message)
        return {"channel": target["id"], "ts": message["ts"], "message": message}

    @staticmethod
    def _metadata(p: dict[str, Any]) -> dict[str, Any]:
        raw = p["metadata"]
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict) or not value.get("event_type"):
            raise SlackApiError("invalid_metadata_format")
        return value

    @staticmethod
    def _visible(m: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        """Message metadata is only returned with ``include_all_metadata``."""
        if m.get("metadata") and str(p.get("include_all_metadata", "")).lower() not in ("true", "1"):
            return {k: v for k, v in m.items() if k != "metadata"}
        return m

    def m_chat_update(self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None) -> dict[str, Any]:
        target = self._target(team, bot, p.get("channel", ""))
        message = next((m for m in target["messages"] if m["ts"] == p.get("ts")), None)
        if message is None:
            raise SlackApiError("message_not_found")
        if message.get("user") != bot:
            raise SlackApiError("cant_update_message")
        message["text"] = p.get("text", message["text"])
        if p.get("blocks") is not None:
            message["blocks"] = self._blocks(p)
        message["edited"] = True
        return {"channel": target["id"], "ts": message["ts"], "text": message["text"]}

    def m_chat_postEphemeral(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        target = self._target(team, bot, p.get("channel", ""))
        user = p.get("user", "")
        if not target.get("is_im") and user not in target.get("members", []):
            raise SlackApiError("user_not_in_channel")
        ts = new_ts()
        team["ephemeral"].append(
            {
                "channel": target["id"],
                "user": user,
                "ts": ts,
                "text": p.get("text", ""),
                "blocks": self._blocks(p),
                "thread_ts": p.get("thread_ts"),
            }
        )
        return {"message_ts": ts}

    def m_chat_getPermalink(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        self.channel(team, p.get("channel", ""))
        return {
            "channel": p.get("channel"),
            "permalink": f"{self.config.slack_public_url}/ui#{team['id']}/{p.get('channel')}/{p.get('message_ts')}",
        }

    def m_users_info(self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None) -> dict[str, Any]:
        user = team["users"].get(p.get("user", ""))
        if not user:
            raise SlackApiError("user_not_found")
        return {"user": {**user, "team_id": team["id"]}}

    def m_users_lookupByEmail(
        self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None
    ) -> dict[str, Any]:
        email = str(p.get("email", "")).lower()
        user = next((u for u in team["users"].values() if u.get("profile", {}).get("email", "").lower() == email), None)
        if not user:
            raise SlackApiError("users_not_found")
        return {"user": user}

    def m_views_publish(self, team: dict[str, Any], bot: str, p: dict[str, Any], token: str | None) -> dict[str, Any]:
        user = p.get("user_id", "")
        if user not in team["users"]:
            raise SlackApiError("user_not_found")
        view = json.loads(p["view"]) if isinstance(p.get("view"), str) else p.get("view")
        if not isinstance(view, dict) or view.get("type") != "home":
            raise SlackApiError("invalid_arguments")
        team["home"][user] = {**view, "id": "V" + uuid.uuid4().hex[:8].upper()}
        return {"view": team["home"][user]}


async def _params(request: Request) -> dict[str, Any]:
    body = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        return json.loads(body) if body else {}
    return {k: v[0] for k, v in parse_qs(body.decode()).items()}


def build_router(slack: Slack) -> APIRouter:
    router = APIRouter(prefix="/slack")
    cfg = slack.config

    @router.post("/api/{method}")
    async def web_api(method: str, request: Request) -> JSONResponse:
        params = await _params(request)
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or params.get("token")
        basic = None
        if request.headers.get("authorization", "").startswith("Basic "):
            import base64

            user, _, password = base64.b64decode(request.headers["authorization"][6:]).decode().partition(":")
            basic = (user, password)
            token = None
        try:
            with slack.store.lock:
                result = slack.api(method, token, params, basic)
                slack.store.save()
            return JSONResponse(result)
        except SlackApiError as exc:
            return JSONResponse({"ok": False, "error": exc.error}, status_code=exc.status, headers=exc.headers)

    # ------------------------------------------------------------------ OAuth v2

    @router.get("/oauth/v2/authorize", response_class=HTMLResponse)
    def authorize(client_id: str, redirect_uri: str, state: str = "", scope: str = "") -> HTMLResponse:
        if client_id != cfg.slack_client_id:
            return HTMLResponse("<h1>Invalid client_id</h1>", status_code=400)
        options = "".join(
            f'<label><input type="radio" name="team" value="{html.escape(t["id"])}" {"checked" if i == 0 else ""}> {html.escape(t["name"])} ({html.escape(t["id"])})</label><br>'
            for i, t in enumerate(sorted(slack.s["teams"].values(), key=lambda t: (t["installed"], t["name"])))
        )
        scopes = "".join(f"<li>{html.escape(s)}</li>" for s in scope.split(",") if s)
        page = f"""<!doctype html><html><head><title>Install Agent</title><style>body{{font-family:system-ui;max-width:520px;margin:40px auto}}button{{padding:8px 16px}}</style></head>
<body><h1>Agent is requesting permission to access a Slack workspace</h1>
<form method="post" action="{cfg.slack_public_url}/oauth/v2/authorize">
<input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}"><input type="hidden" name="state" value="{html.escape(state)}"><input type="hidden" name="scope" value="{html.escape(scope)}">
<fieldset><legend>Workspace</legend>{options}</fieldset>
<p>It will be able to:</p><ul>{scopes}</ul>
<button type="submit" name="decision" value="allow">Allow</button> <button type="submit" name="decision" value="cancel">Cancel</button>
</form></body></html>"""
        return HTMLResponse(page)

    @router.post("/oauth/v2/authorize")
    async def authorize_decision(request: Request) -> RedirectResponse:
        form = await _params(request)
        redirect_uri, state = form.get("redirect_uri", ""), form.get("state", "")
        if form.get("decision") != "allow":
            return RedirectResponse(
                f"{redirect_uri}?{urlencode({'error': 'access_denied', 'state': state})}", status_code=302
            )
        code = "code-" + secrets.token_hex(12)
        with slack.store.lock:
            team = slack.team(form.get("team", ""))
            installer = next((u["id"] for u in team["users"].values() if not u.get("is_bot")), "U0")
            slack.s["codes"][code] = {
                "team": team["id"],
                "redirect_uri": redirect_uri,
                "scope": form.get("scope", ""),
                "user": installer,
                "expires": time.time() + 600,
            }
            slack.store.save()
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)

    # ------------------------------------------------------------------ UI

    @router.get("/ui", response_class=HTMLResponse)
    def ui() -> HTMLResponse:
        return HTMLResponse(UI.read_text())

    @router.get("/_ui/state")
    def ui_state(team: str, user: str = "", channel: str = "", thread: str = "") -> dict[str, Any]:
        with slack.store.lock:
            teams = [{"id": t["id"], "name": t["name"], "installed": t["installed"]} for t in slack.s["teams"].values()]
            data = slack.s["teams"].get(team)
            if not data:
                return {"teams": teams}
            people = [
                {"id": u["id"], "name": u["real_name"], "email": u.get("profile", {}).get("email")}
                for u in data["users"].values()
                if not u.get("is_bot")
            ]
            me = user or (people[0]["id"] if people else "")
            channels = [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "is_private": c["is_private"],
                    "is_archived": c["is_archived"],
                    "bot_member": data["bot_user_id"] in c["members"],
                    "member": me in c["members"],
                }
                for c in sorted(data["channels"].values(), key=lambda c: c["name"])
                if me in c["members"] or not c["is_private"]
            ]
            dm = slack.im_for(data, me) if me else None
            current = channel or (channels[0]["id"] if channels else "")
            messages: list[dict[str, Any]] = []
            replies: list[dict[str, Any]] = []
            if current:
                source = dm if dm and current == dm["id"] else data["channels"].get(current)
                if source:
                    all_messages = source["messages"]
                    top = [m for m in all_messages if not m.get("thread_ts") or m["thread_ts"] == m["ts"]]
                    for m in top:
                        messages.append(
                            {
                                **m,
                                "reply_count": sum(
                                    1 for r in all_messages if r.get("thread_ts") == m["ts"] and r["ts"] != m["ts"]
                                ),
                            }
                        )
                    if thread:
                        replies = [m for m in all_messages if m.get("thread_ts") == thread and m["ts"] != thread]
            ephemeral = [e for e in data["ephemeral"] if e["user"] == me and e["channel"] == current]
            names = {u["id"]: u["real_name"] for u in data["users"].values()}
            slack.store.save()
            return {
                "teams": teams, "team": {"id": data["id"], "name": data["name"], "installed": data["installed"], "bot_user_id": data["bot_user_id"]},
                "people": people, "me": me, "channels": channels, "dm": {"id": dm["id"]} if dm else None, "channel": current,
                "messages": messages, "thread": thread, "replies": replies, "ephemeral": ephemeral, "names": names, "home": data["home"].get(me),
            }  # fmt: skip

    @router.post("/_ui/post")
    async def ui_post(request: Request) -> JSONResponse:
        p = await request.json()
        try:
            with slack.store.lock:
                message = slack.user_post(p["team"], p["user"], p["channel"], p["text"], p.get("thread_ts") or None)
                slack.store.save()
            return JSONResponse({"ok": True, "message": message})
        except SlackApiError as exc:
            return JSONResponse({"ok": False, "error": exc.error}, status_code=400)

    @router.post("/_ui/action")
    async def ui_action(request: Request) -> JSONResponse:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            user = team["users"][p["user"]]
            action: dict[str, Any] = {
                "action_id": p["action_id"],
                "block_id": p.get("block_id", ""),
                "action_ts": new_ts(),
                "type": p.get("type", "button"),
            }
            if p.get("value") is not None:
                action["value"] = p["value"]
            if p.get("selected"):
                action["selected_option"] = {
                    "value": p["selected"],
                    "text": {"type": "plain_text", "text": p["selected"]},
                }
            container = (
                {"type": "view", "view_id": (team["home"].get(user["id"]) or {}).get("id")}
                if p.get("surface") == "home"
                else {
                    "type": "message",
                    "message_ts": p.get("ts"),
                    "channel_id": p.get("channel"),
                    "is_ephemeral": False,
                    **slack.thread_of(team, p.get("channel"), p.get("ts")),
                }
            )
            payload = {
                "type": "block_actions", "user": {"id": user["id"], "username": user["name"], "team_id": team["id"]}, "api_app_id": APP_ID,
                "team": {"id": team["id"], "domain": team["domain"]}, "container": container, "trigger_id": uuid.uuid4().hex,
                "channel": {"id": p.get("channel")} if p.get("channel") else None, "actions": [action],
            }  # fmt: skip
            slack.emit_interaction(team, payload)
        return JSONResponse({"ok": True})

    @router.post("/_ui/home")
    async def ui_home(request: Request) -> JSONResponse:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            dm = slack.im_for(team, p["user"])
            slack.emit_event(team, {"type": "app_home_opened", "user": p["user"], "channel": dm["id"], "tab": "home"})
            slack.store.save()
        return JSONResponse({"ok": True})

    @router.post("/_ui/invite-bot")
    async def ui_invite_bot(request: Request) -> JSONResponse:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            slack.join(team["id"], p["channel"], team["bot_user_id"])  # emits member_joined_channel for the bot
            slack.store.save()
        return JSONResponse({"ok": True})

    @router.post("/_ui/join")
    async def ui_join(request: Request) -> JSONResponse:
        p = await request.json()
        with slack.store.lock:
            (slack.join if p.get("join", True) else slack.leave)(p["team"], p["channel"], p["user"])
            slack.store.save()
        return JSONResponse({"ok": True})

    return router


def build_admin_router(slack: Slack) -> APIRouter:
    router = APIRouter(prefix="/_admin/slack")

    @router.post("/teams")
    async def add_team(request: Request) -> dict[str, Any]:
        """A new, not yet installed workspace (tests that install the app need their own)."""
        p = await request.json()
        team_id = p.get("id") or "T" + uuid.uuid4().hex[:8].upper()
        with slack.store.lock:
            people = [(u["id"], u["email"], u.get("name", u["email"])) for u in p.get("people", [])]
            channels = [
                (c["id"], c["name"], c.get("is_private", False), c.get("members", [])) for c in p.get("channels", [])
            ]
            slack._seed_team(
                team_id,
                p.get("name", team_id),
                p.get("bot_user_id", "U" + team_id[1:] + "BOT"),
                installed=False,
                people=people,
                channels=channels,
            )
            slack.store.save()
        return {"id": team_id}

    @router.post("/users")
    async def add_user(request: Request) -> dict[str, Any]:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            user_id = p.get("id") or "U" + uuid.uuid4().hex[:8].upper()
            team["users"][user_id] = slack._user(user_id, p["email"], p.get("name", p["email"]))
            for channel_id in p.get("channels", []):
                slack.join(team["id"], channel_id, user_id)
            slack.store.save()
        return {"id": user_id}

    @router.post("/channels")
    async def add_channel(request: Request) -> dict[str, Any]:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            channel_id = p.get("id") or "C" + uuid.uuid4().hex[:8].upper()
            team["channels"][channel_id] = {
                "id": channel_id,
                "name": p["name"],
                "is_private": p.get("is_private", False),
                "is_archived": False,
                "members": list(p.get("members", [])),
                "messages": [],
            }
            slack.emit_event(
                team,
                {
                    "type": "group_created" if p.get("is_private") else "channel_created",
                    "channel": {"id": channel_id, "name": p["name"], "created": int(time.time())},
                },
            )
            slack.store.save()
        return {"id": channel_id}

    @router.post("/channels/{channel_id}/rename")
    async def rename(channel_id: str, request: Request) -> dict[str, bool]:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            channel = slack.channel(team, channel_id)
            channel["name"] = p["name"]
            slack.emit_event(
                team,
                {
                    "type": "group_rename" if channel["is_private"] else "channel_rename",
                    "channel": {"id": channel_id, "name": p["name"], "created": int(time.time())},
                },
            )
            slack.store.save()
        return {"ok": True}

    @router.post("/channels/{channel_id}/archive")
    async def archive(channel_id: str, request: Request) -> dict[str, bool]:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            channel = slack.channel(team, channel_id)
            channel["is_archived"] = True
            slack.emit_event(
                team,
                {
                    "type": "group_archive" if channel["is_private"] else "channel_archive",
                    "channel": channel_id,
                    "user": p.get("user", "U0ALICE"),
                },
            )
            slack.store.save()
        return {"ok": True}

    @router.post("/events")
    async def inject(request: Request) -> dict[str, bool]:
        """Deliver an arbitrary event (tests)."""
        p = await request.json()
        with slack.store.lock:
            slack.emit_event(slack.team(p["team"]), p["event"])
        return {"ok": True}

    @router.post("/uninstall")
    async def uninstall(request: Request) -> dict[str, bool]:
        p = await request.json()
        with slack.store.lock:
            team = slack.team(p["team"])
            slack.emit_event(team, {"type": "app_uninstalled"})
            slack.emit_event(team, {"type": "tokens_revoked", "tokens": {"oauth": [], "bot": [team["bot_user_id"]]}})
            slack.store.save()
        await slack.deliverer.drain()
        with slack.store.lock:
            team["installed"] = False
            slack.s["tokens"] = {k: v for k, v in slack.s["tokens"].items() if v["team"] != team["id"]}
            slack.store.save()
        return {"ok": True}

    @router.post("/rate-limits")
    async def rate_limits(request: Request) -> dict[str, Any]:
        p = await request.json()
        with slack.store.lock:
            slack.s["rate_limits"] = p
            slack.store.save()
        return {"ok": True, "rate_limits": p}

    @router.get("/deliveries")
    def deliveries() -> list[dict[str, Any]]:
        return [
            {"kind": d.kind, "summary": d.summary, "delivered": d.delivered, "attempts": d.attempts}
            for d in slack.deliverer.log
        ]

    @router.post("/deliveries/drain")
    async def drain() -> dict[str, int]:
        await slack.deliverer.drain()
        return {"undelivered": sum(1 for d in slack.deliverer.log if not d.delivered)}

    @router.get("/messages")
    def messages(team: str, channel: str) -> list[dict[str, Any]]:
        """Everything in a channel or DM, including replies (tests)."""
        with slack.store.lock:
            data = slack.team(team)
            if channel.startswith("U"):
                return list(slack.im_for(data, channel)["messages"])
            return list(slack.channel(data, channel).get("messages", []))

    @router.get("/ephemeral")
    def ephemeral(team: str, user: str) -> list[dict[str, Any]]:
        with slack.store.lock:
            return [e for e in slack.team(team)["ephemeral"] if e["user"] == user]

    @router.get("/home")
    def home(team: str, user: str) -> dict[str, Any] | None:
        with slack.store.lock:
            return slack.team(team)["home"].get(user)  # type: ignore[no-any-return]

    return router
