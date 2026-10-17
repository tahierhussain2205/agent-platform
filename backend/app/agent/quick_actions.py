"""Buttons on the agent's answers (D-205): the demo's next step, one click away. A click starts a
request in the same thread, as the person who clicked, with a request our code wrote: the button's
intent is a key, never text, and maps to a playbook and to these words. Every check still runs
(identity, channel, roles, policy)."""

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuickAction:
    key: str
    playbook: str
    label: str  # the button
    chose: str  # "<@U> chose ..." once clicked
    request: str  # what the person asks for by clicking


QUICK_ACTIONS = {
    "investigate": QuickAction(
        "investigate",
        "investigate_issue",
        "Plan a fix",
        "Plan a fix",
        "Investigate the issues in the Jira tickets created in this thread and write a detailed plan "
        "to fix them.",
    ),
    "implement": QuickAction(
        "implement",
        "implement_change",
        "Implement this plan",
        "Implement this plan",
        "Implement the plan in this thread: make the change, run the tests, commit and push it to a new "
        "branch, and open a pull request.",
    ),
}
ACTION_ID = "quick_action"


def button(key: str, channel: str, thread_ts: str | None) -> tuple[str, str, str]:
    """The button for ``key``, carrying where its request runs (a private answer's button still
    starts the request in the original thread)."""
    action = QUICK_ACTIONS[key]
    return (
        ACTION_ID,
        action.label,
        json.dumps({"i": key, "c": channel, "t": thread_ts}, separators=(",", ":")),
    )


def parse(value: str) -> tuple[QuickAction, str, str | None] | None:
    """A button's value back: the action, the channel and the thread; None when it is not ours."""
    try:
        data: Any = json.loads(value)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    action = QUICK_ACTIONS.get(str(data.get("i")))
    channel = data.get("c")
    thread = data.get("t")
    if (
        action is None
        or not isinstance(channel, str)
        or not channel
        or not (thread is None or isinstance(thread, str))
    ):
        return None
    return action, channel, thread
