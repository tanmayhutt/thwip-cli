"""Shared helpers for adapters that drive installed CLIs with their own sign-ins."""

from __future__ import annotations

import re
from datetime import UTC

from thwip.agents.base import LimitStatus

_SECRET_PATTERN = re.compile(r"(?i)(?:sk-|key-|token|bearer\s+)?[A-Za-z0-9_\-]{32,}")
_LIMIT_PATTERN = re.compile(
    r"(?i)rate.?limit|usage.?limit|quota|resource_exhausted|too many requests|\b429\b|insufficient.?(?:quota|credits)|"
    r"limit (?:has been )?(?:reached|exceeded)|out of credits"
)


def scrub(text: str, limit: int = 300) -> str:
    """Bound diagnostic text and hide anything shaped like a credential."""
    cleaned = _SECRET_PATTERN.sub("[redacted]", str(text or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def classify_limit(text: str) -> LimitStatus | None:
    """Return the limit category described by a provider error, if any."""
    if not text or not _LIMIT_PATTERN.search(text):
        return None
    lowered = text.lower()
    if "quota" in lowered or "usage" in lowered or "credits" in lowered or "exhausted" in lowered:
        return LimitStatus.QUOTA_EXHAUSTED
    return LimitStatus.RATE_LIMITED


def build_native_prompt(messages: list[dict], system_prompt: str | None) -> str:
    """Render portable text history as a transcript for a fresh native session.

    Only completed user and assistant text turns are included. The final user
    message is the request; earlier turns are context that was carried over from
    other providers. Tool transcripts never reach the native CLI.
    """
    lines: list[str] = []
    portable = [m for m in messages if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)]
    history, latest = portable[:-1], portable[-1] if portable else None
    if history:
        lines.append("You are continuing a conversation that was carried over from another assistant. "
                     "Earlier turns below are context only. Respond to the final user message.")
        if system_prompt:
            lines.append("")
            lines.append("Conversation instructions:")
            lines.append(system_prompt.strip())
        lines.append("")
        lines.append("Earlier conversation:")
        for message in history:
            speaker = "User" if message["role"] == "user" else "Assistant"
            lines.append(f"[{speaker}]")
            lines.append(message["content"].strip())
            lines.append("")
        lines.append("Final user message:")
    elif system_prompt:
        lines.append("Conversation instructions:")
        lines.append(system_prompt.strip())
        lines.append("")
    if latest:
        lines.append(latest["content"].strip())
    return "\n".join(lines).strip()


def describe_limit_windows(windows: list[dict]) -> str:
    """Render reported usage windows such as '5h: 2% used, resets 14:30 | 7d: 23% used'."""
    from datetime import datetime

    parts = []
    for window in windows:
        label = window.get("label") or "window"
        percent = window.get("used_percent")
        text = f"{label}: {percent:.0f}% used" if isinstance(percent, (int, float)) else f"{label}: unknown"
        resets = window.get("resets_at")
        if isinstance(resets, (int, float)) and resets > 0:
            text += f", resets {datetime.fromtimestamp(resets, tz=UTC).astimezone().strftime('%d %b %H:%M')}"
        parts.append(text)
    return " | ".join(parts) or "Not reported yet"


def window_label(minutes) -> str:
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return "window"
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}d"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes)}m"


_NETWORK_PATTERN = re.compile(r"(?i)connection reset|read tcp|dial tcp|connection refused|no such host|tls handshake|timeout awaiting|network is unreachable|EOF$")


def network_hint(text: str) -> str:
    """Explain transport failures between the CLI and its backend; these are not model or login errors."""
    if text and _NETWORK_PATTERN.search(text):
        return (" The CLI could not reach its backend. This is usually the local network or a firewall "
                "resetting the connection; try again, or switch to another network or a VPN.")
    return ""
