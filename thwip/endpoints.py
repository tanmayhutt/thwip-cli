"""Provider endpoint overrides.

Set `THWIP_<PROVIDER>_BASE_URL` (for example `THWIP_OPENAI_BASE_URL`) or an
`[endpoints]` table in `~/.thwip/config.toml` to point a direct adapter at a
different server: a proxy, a self-hosted gateway, a local test double, or any
OpenAI-compatible service. Keys stay provider keys; only the URL changes.
"""

from __future__ import annotations

import os

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "claude": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "deepseek": "https://api.deepseek.com",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

_ENV_NAMES = {"claude": "ANTHROPIC", "google": "GOOGLE"}
_configured: dict[str, str] = {}


def configure(endpoints: dict[str, str] | None) -> None:
    """Install endpoint overrides loaded from config.toml."""
    _configured.clear()
    for provider, url in (endpoints or {}).items():
        if isinstance(url, str) and url.strip():
            _configured[provider.lower()] = url.strip().rstrip("/")


def base_url(provider: str) -> str:
    """Return the effective base URL for a provider (environment wins over config)."""
    env_name = f"THWIP_{_ENV_NAMES.get(provider, provider).upper()}_BASE_URL"
    override = os.environ.get(env_name, "").strip().rstrip("/")
    return override or _configured.get(provider) or DEFAULT_BASE_URLS[provider]


def is_overridden(provider: str) -> bool:
    return base_url(provider) != DEFAULT_BASE_URLS[provider]
