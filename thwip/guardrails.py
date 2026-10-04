"""Output guardrails: checks on what a model produced, independent of which model produced it.

Today's checks are deliberately narrow and deterministic:
- secrets: strings shaped like API keys or tokens in an answer are masked before the answer is
  stored or filed anywhere, and the user is told. A model that echoes a key it saw in a file would
  otherwise write that key into the session history and, via /memory update, into the vault.
- empty answer: a provider that returns only whitespace is reported instead of silently saved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SECRET_PATTERNS = [
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9_\-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("Groq key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{20,}")),
]


@dataclass
class GuardrailResult:
    text: str
    findings: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.findings)


def mask_secrets(text: str) -> GuardrailResult:
    """Replace likely secrets with a label; report what kinds were found."""
    findings: list[str] = []
    masked = text
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(masked):
            findings.append(label)
            masked = pattern.sub(f"[{label} redacted]", masked)
    return GuardrailResult(text=masked, findings=findings)


def check_output(text: str) -> GuardrailResult:
    """All output checks in one call; `text` is what will be shown and stored."""
    if not text.strip():
        return GuardrailResult(text=text, findings=["empty answer"])
    return mask_secrets(text)
