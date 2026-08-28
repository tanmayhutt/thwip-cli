"""
Claude Code agent adapter (Anthropic).

Full capabilities: chat, file editing, code execution, terminal, git.
Detects Claude Code CLI installation and Anthropic API keys.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from thwip.agents.base import (
    AgentDone,
    AgentEvent,
    BaseAgent,
    Capability,
    LimitHit,
    LimitStatus,
    ModelInfo,
    SubscriptionInfo,
    SubscriptionTier,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolUseStart,
)


class ClaudeAgent(BaseAgent):
    """
    Anthropic Claude Code: full coding agent.
    Capabilities: chat plus Thwip's local file, terminal, and Git tools.
    Uses the Anthropic Python SDK for API communication.
    """

    name = "claude"
    display_name = "Claude Code"
    company = "Anthropic"
    description = "Anthropic's agentic coding assistant with full terminal and file access"
    website = "https://claude.ai/code"

    capabilities = {
        Capability.CHAT,
        Capability.FILE_EDIT,
        Capability.FILE_READ,
        Capability.CODE_RUN,
        Capability.TERMINAL,
        Capability.GIT,
    }

    available_models = [
        ModelInfo(
            id="claude-opus-5",
            name="Claude Opus 5",
            tier="flagship",
            description="Complex agentic coding and enterprise work",
            context_window=1_000_000,
            max_output=128_000,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            is_default=True,
            pricing_input=5.0,
            pricing_output=25.0,
        ),
        ModelInfo(
            id="claude-fable-5",
            name="Claude Fable 5",
            tier="flagship",
            description="Highest capability for long-running agents and deep reasoning",
            context_window=1_000_000,
            max_output=128_000,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            pricing_input=10.0,
            pricing_output=50.0,
        ),
        ModelInfo(
            id="claude-sonnet-5",
            name="Claude Sonnet 5",
            tier="balanced",
            description="Best balance of intelligence and speed for production coding",
            context_window=1_000_000,
            max_output=128_000,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            pricing_input=3.0,
            pricing_output=15.0,
        ),
        ModelInfo(
            id="claude-haiku-4-5-20251001",
            name="Claude Haiku 4.5",
            tier="fast",
            description="Fastest Claude model with near-frontier intelligence",
            context_window=200_000,
            max_output=64_000,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            is_default=False,
            pricing_input=1.0,
            pricing_output=5.0,
        ),
    ]

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = None
        self._last_limit_status = LimitStatus.UNKNOWN
        self._install_cache: dict[str, str] | None = None

    def _get_api_key(self) -> str | None:
        """Resolve API key from multiple sources."""
        if self._api_key:
            return self._api_key

        # 1. Environment variable
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if key:
            return key

        # 2. Claude Code config files
        config_paths = [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "config.json",
            Path.home() / ".config" / "claude" / "config.json",
        ]
        for p in config_paths:
            if p.is_file():
                try:
                    data = json.loads(p.read_text())
                    if isinstance(data, dict):
                        k = data.get("apiKey") or data.get("api_key") or ""
                        if k:
                            return k
                except (json.JSONDecodeError, OSError):
                    continue

        return None

    def _ensure_client(self) -> Any:
        """Lazily initialize the Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                key = self._get_api_key()
                if key:
                    self._client = anthropic.AsyncAnthropic(api_key=key)
                else:
                    self._client = anthropic.AsyncAnthropic()  # Will use env var
            except ImportError:
                raise RuntimeError(
                    "anthropic package not installed. Run: pip install anthropic"
                )
        return self._client

    # --- Detection ---

    def is_installed(self) -> bool:
        """Check if Claude Code CLI or Claude desktop app is installed.

        The ~/.claude directory alone is not sufficient since other IDEs
        (like Antigravity) may create it.
        """
        if self.is_configured():
            return True
        if shutil.which("claude") is not None:
            return True
        app_paths = [
            Path("/Applications/Claude.app"),
            Path.home() / "Applications" / "Claude.app",
        ]
        return any(p.exists() for p in app_paths)

    def _has_cli_auth(self) -> dict[str, str] | None:
        """Check for Claude Code subscription auth.

        Requires the `claude` CLI binary or Claude.app to be present.
        The ~/.claude directory alone is not sufficient since other IDEs
        (like Antigravity) may create it.
        """
        has_cli = shutil.which("claude") is not None
        has_app = Path("/Applications/Claude.app").exists() or (Path.home() / "Applications" / "Claude.app").exists()

        if not has_cli and not has_app:
            return None

        claude_dir = Path.home() / ".claude"
        if not claude_dir.is_dir():
            return None

        # Claude Code stores session data when authenticated
        auth_indicators = [
            claude_dir / "projects",
            claude_dir / "backups",
            claude_dir / "debug",
        ]
        has_sessions = any(p.is_dir() and any(p.iterdir()) for p in auth_indicators if p.exists())

        if has_sessions:
            return {"method": "subscription", "account": "Anthropic Max"}

        return None

    @property
    def auth_method(self) -> str:
        if self._get_api_key():
            return "api_key"
        if self._has_cli_auth():
            return "subscription"
        return "none"

    def is_configured(self) -> bool:
        """Return whether the SDK adapter has credentials it can actually use."""
        return bool(self._get_api_key())

    def get_install_info(self) -> dict[str, str]:
        """Get Claude Code installation details."""
        if self._install_cache is not None:
            return self._install_cache

        info: dict[str, str] = {"method": "not installed", "path": "", "version": ""}

        path = shutil.which("claude")
        if path:
            info["path"] = path
            info["method"] = "CLI binary"

            # Try to get version
            try:
                result = subprocess.run(
                    ["claude", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    info["version"] = result.stdout.strip().split("\n")[0]
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        if not info["path"]:
            app_paths = [
                Path("/Applications/Claude.app"),
                Path.home() / "Applications" / "Claude.app",
            ]
            for app in app_paths:
                if app.exists():
                    info["method"] = "macOS config" if app.name == ".claude" else "macOS app"
                    info["path"] = str(app)
                    info["version"] = "Installed"
                    break

        self._install_cache = info
        return info

    def get_subscription_info(self) -> SubscriptionInfo:
        """
        Determine subscription status.
        Checks API key first, then CLI subscription auth.
        """
        key = self._get_api_key()
        if key:
            if key.startswith("sk-ant-"):
                return SubscriptionInfo(
                    tier=SubscriptionTier.PRO,
                    is_active=True,
                    message="Anthropic API key detected",
                )
            return SubscriptionInfo(
                tier=SubscriptionTier.UNKNOWN,
                is_active=True,
                message="API key detected",
            )

        cli_auth = self._has_cli_auth()
        if cli_auth:
            account = cli_auth.get("account", "Active")
            return SubscriptionInfo(
                tier=SubscriptionTier.PRO,
                is_active=True,
                message=f"Claude Code ({account})",
            )

        return SubscriptionInfo(
            tier=SubscriptionTier.UNKNOWN,
            is_active=False,
            message="No credentials found",
        )

    # --- Chat ---

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        """Stream a chat response from Claude."""
        import anthropic

        client = self._ensure_client()
        model = model or self.get_default_model()
        if tools:
            stream = False

        # Build request kwargs
        model_info = self.get_model_info(model)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": model_info.max_output if model_info else 16_384,
        }

        if system_prompt:
            kwargs["system"] = system_prompt

        if tools:
            kwargs["tools"] = tools

        # Check if model supports extended thinking
        if model_info and model_info.supports_thinking:
            # Enable extended thinking for supported models
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10_000}

        try:
            if stream:
                async with client.messages.stream(**kwargs) as response:
                    async for event in response:
                        if hasattr(event, "type"):
                            if event.type == "content_block_delta":
                                if hasattr(event.delta, "text"):
                                    yield TextDelta(content=event.delta.text)
                                elif hasattr(event.delta, "thinking"):
                                    yield ThinkingDelta(content=event.delta.thinking)
                            elif event.type == "content_block_start":
                                if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                                    yield ToolUseStart(
                                        tool_id=event.content_block.id,
                                        tool_name=event.content_block.name,
                                        args={},
                                    )
                            elif event.type == "message_stop":
                                usage = getattr(response, "usage", None) or getattr(
                                    response, "current_message_snapshot", None
                                )
                                tu = TokenUsage()
                                if usage:
                                    msg = response.current_message_snapshot
                                    if msg and hasattr(msg, "usage"):
                                        tu = TokenUsage(
                                            input_tokens=msg.usage.input_tokens,
                                            output_tokens=msg.usage.output_tokens,
                                        )
                                yield AgentDone(usage=tu)

                self._last_limit_status = LimitStatus.OK
            else:
                response = await client.messages.create(**kwargs)
                for block in response.content:
                    if hasattr(block, "text"):
                        yield TextDelta(content=block.text)
                    elif hasattr(block, "type") and block.type == "tool_use":
                        yield ToolUseStart(
                            tool_id=block.id,
                            tool_name=block.name,
                            args=block.input,
                        )
                yield AgentDone(
                    usage=TokenUsage(
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                    ),
                    stop_reason=response.stop_reason or "end_turn",
                )
                self._last_limit_status = LimitStatus.OK

        except anthropic.RateLimitError as e:
            self._last_limit_status = LimitStatus.RATE_LIMITED
            retry_after = None
            if hasattr(e, "response") and e.response:
                retry_after_str = e.response.headers.get("retry-after")
                if retry_after_str:
                    try:
                        retry_after = float(retry_after_str)
                    except ValueError:
                        pass
            yield LimitHit(
                error_type=LimitStatus.RATE_LIMITED,
                retry_after=retry_after,
                message=str(e),
            )

        except anthropic.APIStatusError as e:
            error_body = getattr(e, "body", {}) or {}
            error_msg = ""
            if isinstance(error_body, dict):
                err = error_body.get("error", {})
                error_msg = err.get("message", str(e)) if isinstance(err, dict) else str(e)
            else:
                error_msg = str(e)

            # Check for quota/spend limit exhaustion
            if "spend_limit" in error_msg.lower() or "quota" in error_msg.lower():
                self._last_limit_status = LimitStatus.QUOTA_EXHAUSTED
                yield LimitHit(
                    error_type=LimitStatus.QUOTA_EXHAUSTED,
                    message=error_msg,
                )
            else:
                self._last_limit_status = LimitStatus.UNKNOWN
                yield LimitHit(
                    error_type=LimitStatus.UNKNOWN,
                    message=error_msg,
                )

    # --- Limits ---

    def check_limits(self) -> LimitStatus:
        """Return last known limit status."""
        if not self.is_configured():
            return LimitStatus.NO_KEY
        return self._last_limit_status if self._last_limit_status != LimitStatus.UNKNOWN else LimitStatus.OK
