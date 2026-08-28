"""
Claude Code agent adapter (Anthropic).

Full capabilities: chat, file editing, code execution, terminal, git.
Detects Claude Code CLI installation and Anthropic API keys.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator

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
    Capabilities: Chat, file editing, code execution, terminal, git, browser, search.
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
        Capability.BROWSER,
        Capability.SEARCH,
    }

    available_models = [
        ModelInfo(
            id="claude-sonnet-4",
            name="Claude Sonnet 4",
            context_window=200_000,
            max_output=16_384,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            is_default=True,
            pricing_input=3.0,
            pricing_output=15.0,
        ),
        ModelInfo(
            id="claude-opus-4",
            name="Claude Opus 4",
            context_window=200_000,
            max_output=32_000,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            pricing_input=15.0,
            pricing_output=75.0,
        ),
        ModelInfo(
            id="claude-haiku-3.5",
            name="Claude Haiku 3.5",
            context_window=200_000,
            max_output=8_192,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            is_default=False,
            pricing_input=0.80,
            pricing_output=4.0,
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
        """Check if Claude Code CLI or Claude desktop app is installed or configured."""
        if self.is_configured():
            return True
        if shutil.which("claude") is not None:
            return True
        app_paths = [
            Path("/Applications/Claude.app"),
            Path.home() / "Applications" / "Claude.app",
            Path.home() / ".claude",
        ]
        return any(p.exists() for p in app_paths)

    def is_configured(self) -> bool:
        """Check if we have a valid Anthropic API key."""
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
                Path.home() / ".claude",
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
        We can't fully detect tier without an API call, but we can infer from
        the key prefix and check basic validity.
        """
        key = self._get_api_key()
        if not key:
            return SubscriptionInfo(
                tier=SubscriptionTier.UNKNOWN,
                is_active=False,
                message="No API key found",
            )

        # Anthropic keys start with "sk-ant-"
        if key.startswith("sk-ant-"):
            return SubscriptionInfo(
                tier=SubscriptionTier.PRO,  # Assume pro if they have a key
                is_active=True,
                message="API key detected",
            )

        return SubscriptionInfo(
            tier=SubscriptionTier.UNKNOWN,
            is_active=True,
            message="API key format not recognized, but may work",
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

        # Build request kwargs
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 16_384,
        }

        if system_prompt:
            kwargs["system"] = system_prompt

        if tools:
            kwargs["tools"] = tools

        # Check if model supports extended thinking
        model_info = self.get_model_info(model)
        if model_info and model_info.supports_thinking:
            # Enable extended thinking for supported models
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10_000}
            kwargs["max_tokens"] = 16_384 + 10_000

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
