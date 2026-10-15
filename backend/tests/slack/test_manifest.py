"""The Slack app manifest matches what the code asks for and handles."""

from pathlib import Path

import yaml

from app.api.slack import AGENT_EVENTS, CHANNEL_EVENTS
from app.slack.service import BOT_SCOPES

MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[3] / "slack" / "manifest.yaml").read_text())


def test_scopes_match_the_install_flow() -> None:
    assert sorted(MANIFEST["oauth_config"]["scopes"]["bot"]) == sorted(BOT_SCOPES)
    assert "user" not in MANIFEST["oauth_config"]["scopes"]  # reads use the bot token (decision T6)


def test_every_subscribed_event_is_routed() -> None:
    handled = (
        {"app_mention", "message.im"}
        | CHANNEL_EVENTS
        | {"app_home_opened", "app_uninstalled", "tokens_revoked"}
    )
    assert set(MANIFEST["settings"]["event_subscriptions"]["bot_events"]) <= handled
    assert {"app_mention", "message"} == AGENT_EVENTS


def test_marketplace_compatible_settings() -> None:
    settings = MANIFEST["settings"]
    assert settings["socket_mode_enabled"] is False  # Socket Mode apps cannot be listed
    assert settings["interactivity"]["is_enabled"] is True
    assert settings["event_subscriptions"]["request_url"].endswith("/slack/events")
    assert MANIFEST["features"]["app_home"]["home_tab_enabled"] is True
