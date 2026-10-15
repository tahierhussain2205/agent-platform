"""App factory: mounts each stand-in under its own prefix, plus admin endpoints for tests."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from starlette.middleware.cors import CORSMiddleware

from fakes.anthropic import Anthropic
from fakes.anthropic import build_router as build_anthropic_router
from fakes.clerk import Clerk, build_router, clerk_error_handler
from fakes.config import Config
from fakes.slack import Slack, SlackDeliverer
from fakes.slack import build_admin_router as build_slack_admin_router
from fakes.slack import build_router as build_slack_router
from fakes.store import Store
from fakes.svix import Deliverer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(config: Config | None = None, persist: bool = True) -> FastAPI:
    config = config or Config()
    store = Store(config.state_dir / "state.json" if persist else None)
    deliverer = Deliverer(config.clerk_webhook_url, config.clerk_webhook_secret)
    clerk = Clerk(config, store, deliverer)
    slack_deliverer = SlackDeliverer(config.slack_signing_secret)
    slack = Slack(config, store, slack_deliverer)
    anthropic = Anthropic(config)

    def fixture() -> dict[str, Any] | None:
        return json.loads(config.seed_fixture.read_text()) if config.seed_fixture.exists() else None

    def reset() -> None:
        with store.lock:
            key = store.data.get("clerk", {}).get("key")
            store.data = {}
            clerk.reset(fixture(), keep_key=key)
            slack.reset(fixture())
        anthropic.reset()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not store.load() or "clerk" not in store.data or "slack" not in store.data:
            reset()
        deliverer.start()
        slack_deliverer.start()
        yield
        await deliverer.stop()
        await slack_deliverer.stop()

    app = FastAPI(title="fakes", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.console_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    app.add_exception_handler(HTTPException, clerk_error_handler)  # type: ignore[arg-type]
    app.include_router(build_router(clerk))
    app.include_router(build_slack_router(slack))
    app.include_router(build_slack_admin_router(slack))
    app.include_router(build_anthropic_router(anthropic))
    app.state.clerk = clerk
    app.state.deliverer = deliverer
    app.state.slack = slack
    app.state.slack_deliverer = slack_deliverer
    app.state.anthropic = anthropic

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/_admin/reset")
    async def admin_reset() -> dict[str, str]:
        await deliverer.drain()
        await slack_deliverer.drain()
        reset()
        deliverer.log.clear()
        slack_deliverer.log.clear()
        return {"status": "reset"}

    @app.get("/_admin/clerk/webhooks")
    def admin_webhooks() -> list[dict[str, Any]]:
        return [
            {
                "msg_id": d.msg_id,
                "type": d.event_type,
                "delivered": d.delivered,
                "attempts": d.attempts,
                "data": d.payload["data"],
            }
            for d in deliverer.log
        ]

    @app.post("/_admin/clerk/webhooks/drain")
    async def admin_drain() -> dict[str, int]:
        await deliverer.drain()
        return {"undelivered": sum(1 for d in deliverer.log if not d.delivered and deliverer.url)}

    @app.get("/_admin/clerk/state")
    def admin_state() -> dict[str, Any]:
        with store.lock:
            state: dict[str, Any] = json.loads(json.dumps({k: v for k, v in clerk.s.items() if k != "key"}))
            return state

    return app
