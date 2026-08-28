"""
Base agent adapter: abstract interface that all agent adapters implement.

Defines capabilities, events, limit status, and the contract every
agent (Claude, Antigravity, Codex, etc.) must follow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, AsyncIterator


# ---------------------------------------------------------------------------
# Capabilities: what an agent can do
# ---------------------------------------------------------------------------

class Capability(str, Enum):
    """Things a coding agent can do."""
    CHAT = "chat"
    FILE_EDIT = "file_edit"
    FILE_READ = "file_read"
    CODE_RUN = "code_run"
    TERMINAL = "terminal"
    GIT = "git"
    BROWSER = "browser"
    IMAGE_GEN = "image"
    SEARCH = "search"

    @property
    def display_name(self) -> str:
        names = {
            "chat": "Chat",
            "file_edit": "File Edit",
            "file_read": "File Read",
            "code_run": "Code Run",
            "terminal": "Terminal",
            "git": "Git",
            "browser": "Browser",
            "image": "Image Gen",
            "search": "Web Search",
        }
        return names.get(self.value, self.value)

    @property
    def icon(self) -> str:
        return self.display_name


# All possible capabilities for comparison
ALL_CAPABILITIES = set(Capability)


# ---------------------------------------------------------------------------
# Limit / Subscription Status
# ---------------------------------------------------------------------------

class LimitStatus(str, Enum):
    OK = "ok"                           # All good, can make requests
    RATE_LIMITED = "rate_limited"        # Transient: retry after delay
    QUOTA_EXHAUSTED = "quota_exhausted"  # Hard limit: switch agent
    INVALID_KEY = "invalid_key"          # Bad API key
    NO_KEY = "no_key"                    # No API key configured
    NO_SUBSCRIPTION = "no_subscription"  # Key exists but no active plan
    UNKNOWN = "unknown"                  # Cannot determine


class SubscriptionTier(str, Enum):
    FREE = "Free"
    PRO = "Pro"
    TEAM = "Team"
    ENTERPRISE = "Enterprise"
    UNLIMITED = "Unlimited"     # e.g., Ollama local
    UNKNOWN = "Unknown"


@dataclass
class SubscriptionInfo:
    """Subscription details for an agent/provider."""
    tier: SubscriptionTier = SubscriptionTier.UNKNOWN
    is_active: bool = False
    usage_percent: float | None = None  # 0-100, None if unknown
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    reset_at: datetime | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# Agent Events (streaming response protocol)
# ---------------------------------------------------------------------------

@dataclass
class TextDelta:
    """A chunk of streamed text."""
    content: str


@dataclass
class ThinkingDelta:
    """A chunk of reasoning/thinking content (for reasoning models)."""
    content: str


@dataclass
class ToolUseStart:
    """Agent wants to use a tool."""
    tool_id: str
    tool_name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    """Result from a tool execution."""
    tool_id: str
    output: str
    error: str | None = None


@dataclass
class TokenUsage:
    """Token usage for a response."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LimitHit:
    """Agent hit a rate limit or quota."""
    error_type: LimitStatus
    retry_after: float | None = None  # Seconds to wait (for rate limits)
    message: str = ""


@dataclass
class AgentDone:
    """Agent finished responding."""
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str = "end_turn"


# Union of all possible events
AgentEvent = TextDelta | ThinkingDelta | ToolUseStart | ToolResult | TokenUsage | LimitHit | AgentDone


# ---------------------------------------------------------------------------
# Model Info
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    """Information about a specific model."""
    id: str                     # "claude-sonnet-4"
    name: str                   # "Claude Sonnet 4"
    tier: str = "balanced"      # "flagship" (high), "balanced" (mid), "fast" (low-cost)
    description: str = ""       # e.g. "Flagship reasoning with 1M context"
    context_window: int = 0     # Max tokens
    max_output: int = 0         # Max output tokens
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    supports_thinking: bool = False  # Extended thinking / reasoning
    is_default: bool = False
    pricing_input: float = 0.0   # $ per 1M input tokens
    pricing_output: float = 0.0  # $ per 1M output tokens


# ---------------------------------------------------------------------------
# Base Agent Adapter
# ---------------------------------------------------------------------------

class BaseAgent(ABC):
    """
    Abstract base class for all agent adapters.

    Every coding agent (Claude Code, Antigravity, Codex, Aider, etc.)
    implements this interface. thwip uses it to:
    - Detect if the agent is installed and configured
    - Know what the agent can do (capabilities)
    - Send chat messages and receive streaming responses
    - Check rate limits and subscription status
    """

    # --- Identity ---
    name: str = ""                  # "claude" (short identifier)
    display_name: str = ""          # "Claude Code"
    company: str = ""               # "Anthropic"
    description: str = ""           # Brief description
    website: str = ""               # "https://claude.ai"

    # --- Capabilities ---
    capabilities: set[Capability] = set()
    available_models: list[ModelInfo] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    # --- Detection & Configuration ---

    @abstractmethod
    def is_installed(self) -> bool:
        """Check if this agent's CLI/app is installed on the system."""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if this agent has a valid API key / credentials."""
        ...

    @abstractmethod
    def get_install_info(self) -> dict[str, str]:
        """
        Return installation details.
        {
            "method": "npm global",    # How it's installed
            "path": "/usr/local/bin/claude",  # Where the binary is
            "version": "1.0.20",       # Version if detectable
        }
        """
        ...

    @abstractmethod
    def get_subscription_info(self) -> SubscriptionInfo:
        """
        Check the user's subscription/plan status for this agent.
        Tries to determine tier, usage, and remaining quota.
        """
        ...

    # --- Chat ---

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """
        Send messages and receive streaming agent events.

        Args:
            messages: Conversation history in portable format
                      [{role: "user"|"assistant", content: "..."}]
            model: Model to use (None = default)
            system_prompt: System instruction
            tools: Tool definitions for function calling
            stream: Whether to stream the response

        Yields:
            AgentEvent objects (TextDelta, ToolUseStart, etc.)
        """
        ...

    # --- Limits ---

    @abstractmethod
    def check_limits(self) -> LimitStatus:
        """Check current rate limit / quota status."""
        ...

    # --- Helpers ---

    def get_default_model(self) -> str:
        """Return the default model ID."""
        for m in self.available_models:
            if m.is_default:
                return m.id
        if self.available_models:
            return self.available_models[0].id
        return ""

    def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Get info for a specific model."""
        for m in self.available_models:
            if m.id == model_id:
                return m
        return None

    def has_capability(self, cap: Capability) -> bool:
        """Check if this agent supports a capability."""
        return cap in self.capabilities

    def get_missing_capabilities(self, compared_to: set[Capability]) -> list[str]:
        """Get capabilities that this agent lacks compared to another set."""
        missing = compared_to - self.capabilities
        return [c.display_name for c in sorted(missing, key=lambda x: x.value)]

    def get_capability_names(self) -> list[str]:
        """Get display names of all capabilities."""
        return [c.display_name for c in sorted(self.capabilities, key=lambda x: x.value)]

    def get_status_display(self) -> tuple[str, str]:
        """Return (status_text, style) for table display."""
        if not self.is_installed():
            return ("Not Installed", "dim")
        if not self.is_configured():
            return ("Installed (No Key)", "status.limited")

        limit = self.check_limits()
        if limit == LimitStatus.OK:
            return ("Ready", "status.ready")
        elif limit == LimitStatus.RATE_LIMITED:
            return ("Rate Limited", "status.limited")
        elif limit == LimitStatus.QUOTA_EXHAUSTED:
            return ("Quota Exhausted", "error")
        elif limit == LimitStatus.NO_SUBSCRIPTION:
            return ("No Subscription", "status.no_key")
        elif limit == LimitStatus.INVALID_KEY:
            return ("Invalid Key", "error")
        else:
            return ("Installed", "status.ready")

    def to_table_row(self) -> dict:
        """Convert to a dict suitable for render_agents_table()."""
        status_text, status_style = self.get_status_display()
        sub = self.get_subscription_info()

        models_str = ", ".join(m.id for m in self.available_models[:3])
        if len(self.available_models) > 3:
            models_str += f" +{len(self.available_models) - 3}"

        return {
            "name": self.display_name,
            "company": self.company,
            "status": status_text,
            "status_style": status_style,
            "models": models_str,
            "capabilities": [c.display_name for c in sorted(self.capabilities, key=lambda x: x.value)],
            "missing_capabilities": [],
            "subscription": sub.tier.value if sub.tier != SubscriptionTier.UNKNOWN else (
                "Active" if sub.is_active else "Unknown"
            ),
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} company={self.company!r}>"
