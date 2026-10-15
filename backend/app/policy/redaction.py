"""Secret redaction (spec 9.4): applied to tool output before the model sees it and to every
message before it leaves for Slack. Patterns favour recall for well known token formats; a
redacted value is replaced by a label so the text stays readable."""

import re
from collections.abc import Callable

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("api key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("password in url", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("bearer token", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/-]{20,}=*")),
    # key=value style: keep the key, replace the value (group 3).
    (
        "secret",
        re.compile(
            r"(?i)\b(password|passwd|secret|api_key|apikey|access_token|token)([\"']?\s?[:=]\s?[\"']?)([^\s\"']{8,})"
        ),
    ),
]


def _keep_key(label: str) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[redacted {label}]"

    return replace


def redact(text: str) -> tuple[str, int]:
    """Returns the redacted text and how many values were replaced."""
    count = 0
    for label, pattern in PATTERNS:
        replacement: str | Callable[[re.Match[str]], str] = (
            _keep_key(label) if pattern.groups >= 3 else f"[redacted {label}]"
        )
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count
