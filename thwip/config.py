"""
Configuration management for thwip.

Handles:
- Loading/saving ~/.thwip/config.toml
- Auto-discovering API keys from installed agent configs and env vars
- First-run setup wizard
- Merging keys from all sources
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_config_dir() -> Path:
    """Return ~/.thwip/, creating it if needed."""
    override = os.environ.get("THWIP_CONFIG_DIR", "").strip()
    config_dir = Path(override).expanduser() if override else Path.home() / ".thwip"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        config_dir.chmod(0o700)
    except OSError:
        pass
    return config_dir


def get_sessions_dir() -> Path:
    """Return ~/.thwip/sessions/, creating it if needed."""
    d = get_config_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_path() -> Path:
    return get_config_dir() / "config.toml"


def get_usage_path() -> Path:
    return get_config_dir() / "usage.json"


# ---------------------------------------------------------------------------
# API Key Discovery
# ---------------------------------------------------------------------------

# Map of env var names to provider keys
ENV_KEY_MAP: dict[str, str] = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "GEMINI_API_KEY": "google",
    "GOOGLE_API_KEY": "google",
    "DEEPSEEK_API_KEY": "deepseek",
    "MISTRAL_API_KEY": "mistral",
    "GROQ_API_KEY": "groq",
    "OPENROUTER_API_KEY": "openrouter",
}

# Known agent config file locations where we can find API keys
AGENT_CONFIG_LOCATIONS: list[dict[str, Any]] = [
    {
        "provider": "anthropic",
        "paths": ["~/.claude.json", "~/.claude/config.json", "~/.config/claude/config.json"],
        "key_field": "apiKey",
        "format": "json",
    },
    {
        "provider": "openai",
        "paths": ["~/.config/openai/config.json"],
        "key_field": "api_key",
        "format": "json",
    },
    {
        "provider": "google",
        "paths": [
            "~/.config/gemini/config.json",
            "~/.gemini/config.json",
        ],
        "key_field": "api_key",
        "format": "json",
    },
]


def discover_api_keys() -> dict[str, str]:
    """
    Auto-discover API keys from:
    1. Environment variables
    2. Installed agent config files
    3. thwip's own config

    Returns a dict of {provider: api_key}.
    """
    keys: dict[str, str] = {}

    # --- 1. Environment variables ---
    for env_var, provider in ENV_KEY_MAP.items():
        val = os.environ.get(env_var, "").strip()
        if val:
            keys[provider] = val

    # --- 2. Installed agent config files ---
    for agent_conf in AGENT_CONFIG_LOCATIONS:
        provider = agent_conf["provider"]
        if provider in keys:
            continue  # Already found from env

        for path_str in agent_conf["paths"]:
            path = Path(path_str).expanduser()
            if not path.is_file():
                continue
            try:
                if agent_conf["format"] == "json":
                    data = json.loads(path.read_text())
                    key_field = agent_conf["key_field"]
                    # Support nested keys like {"credentials": {"api_key": "..."}}
                    if isinstance(data, dict):
                        val = data.get(key_field, "")
                        if not val and "credentials" in data:
                            val = data["credentials"].get(key_field, "")
                        if val:
                            keys[provider] = val
                            break
            except (json.JSONDecodeError, KeyError, TypeError, OSError):
                continue

    return keys


def get_key_sources() -> dict[str, str]:
    """Return where each discovered key came from (for display)."""
    sources: dict[str, str] = {}

    for env_var, provider in ENV_KEY_MAP.items():
        if os.environ.get(env_var, "").strip():
            sources[provider] = f"env:{env_var}"

    for agent_conf in AGENT_CONFIG_LOCATIONS:
        provider = agent_conf["provider"]
        if provider in sources:
            continue
        for path_str in agent_conf["paths"]:
            path = Path(path_str).expanduser()
            if path.is_file():
                try:
                    data = json.loads(path.read_text())
                    if data.get(agent_conf["key_field"]):
                        sources[provider] = str(path)
                        break
                except (json.JSONDecodeError, OSError):
                    continue

    return sources


# ---------------------------------------------------------------------------
# Config Dataclass
# ---------------------------------------------------------------------------

@dataclass
class FallbackConfig:
    enabled: bool = True
    chain: list[str] = field(default_factory=lambda: [
        "claude/claude-opus-5",
        "google/gemini-3.7-flash",
        "openai/gpt-5.6-terra",
        "deepseek/deepseek-v4-flash",
        "ollama/llama3.3",
    ])


@dataclass
class DisplayConfig:
    show_agent_badge: bool = True
    show_token_count: bool = True
    show_cost: bool = True
    show_capabilities: bool = True
    markdown: bool = True
    syntax_highlight: bool = True
    max_width: int = 120
    dynamic_ui: bool = True  # Change UI based on agent capabilities


@dataclass
class LimitsConfig:
    warn_at_percent: int = 80
    auto_switch: bool = False  # Prompt user vs auto-switch


@dataclass
class ThwipConfig:
    """Main configuration object."""

    # Defaults
    default_agent: str = "claude"
    default_model: str = "claude-opus-5"
    project: str = "."
    theme: str = "dark"
    stream: bool = True
    auto_save: bool = True
    confirm_tools: bool = True

    # API keys (merged from all sources)
    keys: dict[str, str] = field(default_factory=dict)
    key_sources: dict[str, str] = field(default_factory=dict)

    # Ollama
    ollama_host: str = "http://localhost:11434"

    # Sub-configs
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)

    @classmethod
    def load(cls) -> ThwipConfig:
        """Load config from file, env vars, and agent configs."""
        config = cls()
        config_path = get_config_path()

        # Load from file if exists
        if config_path.is_file():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
                config._apply_toml(data)
            except Exception:
                pass  # Use defaults on parse error

        # Merge API keys from all sources
        discovered = discover_api_keys()
        discovered_sources = get_key_sources()

        # Config file keys take priority, then discovered keys fill gaps
        for provider, key in discovered.items():
            if provider not in config.keys or not config.keys[provider]:
                config.keys[provider] = key
                config.key_sources[provider] = discovered_sources.get(provider, "discovered")

        return config

    def _apply_toml(self, data: dict[str, Any]) -> None:
        """Apply parsed TOML data to config fields."""
        defaults = data.get("defaults", {})
        if "agent" in defaults:
            self.default_agent = defaults["agent"]
        if "model" in defaults:
            self.default_model = defaults["model"]
        if "project" in defaults:
            self.project = defaults["project"]
        if "theme" in defaults:
            self.theme = defaults["theme"]
        if "stream" in defaults:
            self.stream = defaults["stream"]
        if "auto_save" in defaults:
            self.auto_save = defaults["auto_save"]
        if "confirm_tools" in defaults:
            self.confirm_tools = defaults["confirm_tools"]

        # Keys from config file
        keys_data = data.get("keys", {})
        for provider, key in keys_data.items():
            if key:  # Only if non-empty
                self.keys[provider] = key
                self.key_sources[provider] = "config.toml"

        # Ollama
        ollama = data.get("ollama", {})
        if "host" in ollama:
            self.ollama_host = ollama["host"]

        # Fallback
        fallback = data.get("fallback", {})
        if "enabled" in fallback:
            self.fallback.enabled = fallback["enabled"]
        if "chain" in fallback:
            self.fallback.chain = fallback["chain"]

        # Display
        display = data.get("display", {})
        for key in ("show_agent_badge", "show_token_count", "show_cost",
                     "show_capabilities", "markdown", "syntax_highlight",
                     "max_width", "dynamic_ui"):
            if key in display:
                setattr(self.display, key, display[key])

        # Limits
        limits = data.get("limits", {})
        if "warn_at_percent" in limits:
            self.limits.warn_at_percent = limits["warn_at_percent"]
        if "auto_switch" in limits:
            self.limits.auto_switch = limits["auto_switch"]

    def save(self) -> None:
        """Save current config to TOML file."""
        data: dict[str, Any] = {
            "defaults": {
                "agent": self.default_agent,
                "model": self.default_model,
                "project": self.project,
                "theme": self.theme,
                "stream": self.stream,
                "auto_save": self.auto_save,
                "confirm_tools": self.confirm_tools,
            },
            "keys": {k: v for k, v in self.keys.items()},
            "ollama": {
                "host": self.ollama_host,
            },
            "fallback": {
                "enabled": self.fallback.enabled,
                "chain": self.fallback.chain,
            },
            "display": {
                "show_agent_badge": self.display.show_agent_badge,
                "show_token_count": self.display.show_token_count,
                "show_cost": self.display.show_cost,
                "show_capabilities": self.display.show_capabilities,
                "markdown": self.display.markdown,
                "syntax_highlight": self.display.syntax_highlight,
                "max_width": self.display.max_width,
                "dynamic_ui": self.display.dynamic_ui,
            },
            "limits": {
                "warn_at_percent": self.limits.warn_at_percent,
                "auto_switch": self.limits.auto_switch,
            },
        }

        config_path = get_config_path()
        with open(config_path, "wb") as f:
            tomli_w.dump(data, f)
        try:
            config_path.chmod(0o600)
        except OSError:
            pass

    def get_key(self, provider: str) -> str | None:
        """Get API key for a provider, returning None if not found."""
        return self.keys.get(provider)

    def has_key(self, provider: str) -> bool:
        """Check if we have a valid API key for a provider."""
        key = self.keys.get(provider, "")
        return bool(key and key.strip())


def create_default_config() -> None:
    """Create default config file if it doesn't exist."""
    config_path = get_config_path()
    if config_path.is_file():
        return

    config = ThwipConfig()
    # Discover any existing keys
    config.keys = discover_api_keys()
    config.save()
