"""Slack test settings. The shared fixtures (env, stack, api) live in ``tests/stack.py``."""

import pytest

from tests.stack import FAKES, SIGNING_SECRET, Stack, app_for, fakes_up, seed_slack_token

__all__ = ["FAKES", "SIGNING_SECRET", "Stack", "app_for", "fakes_up", "seed_slack_token"]


@pytest.fixture(autouse=True)
def _slack_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("SLACK_CLIENT_ID", "local.client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "local-slack-client-secret")
    monkeypatch.setenv("SLACK_STATE_SECRET", "slack-state-secret-for-tests")
    monkeypatch.setenv("SLACK_API_URL", f"{FAKES}/slack/api")
    monkeypatch.setenv("SLACK_AUTHORIZE_URL", f"{FAKES}/slack/oauth/v2/authorize")
    monkeypatch.setenv("API_PUBLIC_URL", "http://api.test")
    monkeypatch.setenv("CONSOLE_URL", "http://console.test")
