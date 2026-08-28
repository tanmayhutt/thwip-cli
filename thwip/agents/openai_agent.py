"""
OpenAI / Codex agent adapter.

Full capabilities: chat, file editing, code execution (sandboxed).
Detects Codex CLI, OpenAI API keys.
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


class OpenAIAgent(BaseAgent):
    """
    OpenAI Codex / ChatGPT: coding agent.

    Capabilities: Chat, file editing, code execution (sandboxed).
    Uses the OpenAI Python SDK.
    """

    name = "openai"
    display_name = "ChatGPT / Codex"
    company = "OpenAI"
    description = "OpenAI's ChatGPT and Codex models with tool execution"
    website = "https://platform.openai.com"

    capabilities = {
        Capability.CHAT,
        Capability.FILE_EDIT,
        Capability.FILE_READ,
        Capability.CODE_RUN,
        Capability.TERMINAL,
        Capability.IMAGE_GEN,
        Capability.SEARCH,
    }

    available_models = [
        ModelInfo(
            id="gpt-4.1",
            name="GPT-4.1",
            tier="flagship",
            description="Next-generation flagship with 1M context and advanced coding",
            context_window=1_047_576,
            max_output=32_768,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            is_default=True,
            pricing_input=2.0,
            pricing_output=8.0,
        ),
        ModelInfo(
            id="o3",
            name="O3",
            tier="flagship",
            description="Deep reasoning model with autonomous multi-step problem solving",
            context_window=200_000,
            max_output=100_000,
            supports_tools=True,
            supports_streaming=True,
            supports_thinking=True,
            pricing_input=2.0,
            pricing_output=8.0,
        ),
        ModelInfo(
            id="gpt-4o",
            name="GPT-4o",
            tier="balanced",
            description="High-speed multimodal flagship for omni-modal coding and vision",
            context_window=128_000,
            max_output=16_384,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            pricing_input=2.50,
            pricing_output=10.0,
        ),
        ModelInfo(
            id="o4-mini",
            name="O4 Mini",
            tier="balanced",
            description="Fast and affordable reasoning model for everyday programming",
            context_window=200_000,
            max_output=100_000,
            supports_tools=True,
            supports_streaming=True,
            supports_thinking=True,
            pricing_input=1.10,
            pricing_output=4.40,
        ),
        ModelInfo(
            id="gpt-4o-mini",
            name="GPT-4o Mini",
            tier="fast",
            description="Lightweight and low-latency model for fast script tasks",
            context_window=128_000,
            max_output=16_384,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            pricing_input=0.15,
            pricing_output=0.60,
        ),
        ModelInfo(
            id="codex-mini",
            name="Codex Mini",
            tier="fast",
            description="Specialized fast code generation model",
            context_window=200_000,
            max_output=16_384,
            supports_tools=True,
            supports_streaming=True,
            pricing_input=1.50,
            pricing_output=6.0,
        ),
    ]

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = None
        self._last_limit_status = LimitStatus.UNKNOWN

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key

        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if key:
            return key

        config_paths = [
            Path.home() / ".config" / "openai" / "config.json",
            Path.home() / ".openai" / "config.json",
        ]
        for p in config_paths:
            if p.is_file():
                try:
                    data = json.loads(p.read_text())
                    k = data.get("api_key") or data.get("apiKey") or ""
                    if k:
                        return k
                except (json.JSONDecodeError, OSError):
                    continue

        return None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                key = self._get_api_key()
                self._client = AsyncOpenAI(api_key=key) if key else AsyncOpenAI()
            except ImportError:
                raise RuntimeError(
                    "openai package not installed. Run: pip install openai"
                )
        return self._client

    # --- Detection ---

    def is_installed(self) -> bool:
        """Check if Codex CLI, OpenAI CLI, or ChatGPT app is installed."""
        if self.is_configured():
            return True
        if any(shutil.which(cmd) is not None for cmd in ("codex", "openai", "chatgpt")):
            return True
        app_paths = [
            Path("/Applications/ChatGPT.app"),
            Path("/Applications/OpenAI.app"),
            Path.home() / "Applications" / "ChatGPT.app",
            Path.home() / ".config" / "openai",
        ]
        return any(p.exists() for p in app_paths)

    def _has_cli_auth(self) -> dict[str, str] | None:
        """Check for ChatGPT OAuth via Codex CLI's auth.json."""
        auth_file = Path.home() / ".codex" / "auth.json"
        if auth_file.is_file():
            try:
                data = json.loads(auth_file.read_text())
                if data.get("auth_mode") == "chatgpt" and data.get("tokens"):
                    tokens = data["tokens"]
                    result: dict[str, str] = {"method": "chatgpt_oauth"}
                    # Decode plan info from JWT id_token claims (no verification)
                    id_token = tokens.get("id_token", "")
                    if id_token:
                        try:
                            import base64
                            parts = id_token.split(".")
                            if len(parts) >= 2:
                                payload = parts[1]
                                padding = 4 - len(payload) % 4
                                if padding != 4:
                                    payload += "=" * padding
                                claims = json.loads(base64.urlsafe_b64decode(payload))
                                result["account"] = claims.get("email", "")
                                auth_info = claims.get("https://api.openai.com/auth", {})
                                result["plan"] = auth_info.get("chatgpt_plan_type", "")
                        except Exception:
                            pass
                    return result
            except Exception:
                pass
        return None

    @property
    def auth_method(self) -> str:
        if self._get_api_key():
            return "api_key"
        if self._has_cli_auth():
            return "subscription"
        return "none"

    def is_configured(self) -> bool:
        return bool(self._get_api_key()) or bool(self._has_cli_auth())

    def get_install_info(self) -> dict[str, str]:
        info: dict[str, str] = {"method": "not installed", "path": "", "version": ""}

        for cmd in ("codex", "openai", "chatgpt"):
            path = shutil.which(cmd)
            if path:
                info["path"] = path
                info["method"] = "CLI binary"
                try:
                    result = subprocess.run(
                        [cmd, "--version"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0:
                        info["version"] = result.stdout.strip().split("\n")[0]
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                    pass
                break

        # Also check for ChatGPT macOS app
        if not info["path"]:
            app_paths = [
                Path("/Applications/ChatGPT.app"),
                Path("/Applications/OpenAI.app"),
                Path.home() / "Applications" / "ChatGPT.app",
            ]
            for app in app_paths:
                if app.exists():
                    info["method"] = "macOS app"
                    info["path"] = str(app)
                    info["version"] = "Installed"
                    break

        return info

    def get_subscription_info(self) -> SubscriptionInfo:
        key = self._get_api_key()
        if key:
            if key.startswith("sk-"):
                return SubscriptionInfo(
                    tier=SubscriptionTier.PRO,
                    is_active=True,
                    message="OpenAI API key detected",
                )
            return SubscriptionInfo(
                tier=SubscriptionTier.UNKNOWN,
                is_active=True,
                message="API key detected",
            )

        cli_auth = self._has_cli_auth()
        if cli_auth:
            plan = cli_auth.get("plan", "")
            account = cli_auth.get("account", "")
            tier_map = {
                "plus": SubscriptionTier.PRO,
                "pro": SubscriptionTier.PRO,
                "team": SubscriptionTier.TEAM,
                "enterprise": SubscriptionTier.ENTERPRISE,
            }
            tier = tier_map.get(plan, SubscriptionTier.UNKNOWN)
            plan_label = plan.title() if plan else "Active"
            msg = f"ChatGPT {plan_label}"
            if account:
                msg += f" ({account})"
            return SubscriptionInfo(
                tier=tier,
                is_active=True,
                message=msg,
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
        """Stream a chat response from OpenAI."""
        import openai

        client = self._ensure_client()
        model = model or self.get_default_model()

        # Build messages with system prompt
        api_messages: list[dict[str, Any]] = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
        }

        if tools:
            kwargs["tools"] = tools

        # Reasoning models use different params
        model_info = self.get_model_info(model)
        if model_info and model_info.supports_thinking:
            kwargs["max_completion_tokens"] = 100_000
        else:
            kwargs["max_tokens"] = 16_384

        try:
            if stream:
                response = await client.chat.completions.create(stream=True, **kwargs)

                collected_content = ""
                total_usage = TokenUsage()

                async for chunk in response:
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        if delta.content:
                            yield TextDelta(content=delta.content)
                            collected_content += delta.content
                        # Tool calls
                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                if tc.function:
                                    yield ToolUseStart(
                                        tool_id=tc.id or "",
                                        tool_name=tc.function.name or "",
                                        args=json.loads(tc.function.arguments) if tc.function.arguments else {},
                                    )

                    if chunk.usage:
                        total_usage = TokenUsage(
                            input_tokens=chunk.usage.prompt_tokens or 0,
                            output_tokens=chunk.usage.completion_tokens or 0,
                        )

                yield AgentDone(usage=total_usage)
                self._last_limit_status = LimitStatus.OK

            else:
                response = await client.chat.completions.create(**kwargs)

                if response.choices:
                    msg = response.choices[0].message
                    if msg.content:
                        yield TextDelta(content=msg.content)
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            yield ToolUseStart(
                                tool_id=tc.id,
                                tool_name=tc.function.name,
                                args=json.loads(tc.function.arguments) if tc.function.arguments else {},
                            )

                usage = response.usage
                yield AgentDone(
                    usage=TokenUsage(
                        input_tokens=usage.prompt_tokens if usage else 0,
                        output_tokens=usage.completion_tokens if usage else 0,
                    ),
                    stop_reason=response.choices[0].finish_reason if response.choices else "stop",
                )
                self._last_limit_status = LimitStatus.OK

        except openai.RateLimitError as e:
            self._last_limit_status = LimitStatus.RATE_LIMITED
            retry_after = None
            if hasattr(e, "response") and e.response:
                ra = e.response.headers.get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except ValueError:
                        pass
            yield LimitHit(
                error_type=LimitStatus.RATE_LIMITED,
                retry_after=retry_after,
                message=str(e),
            )

        except openai.APIStatusError as e:
            error_str = str(e).lower()
            if "insufficient_quota" in error_str or "quota" in error_str:
                self._last_limit_status = LimitStatus.QUOTA_EXHAUSTED
                yield LimitHit(
                    error_type=LimitStatus.QUOTA_EXHAUSTED,
                    message=str(e),
                )
            else:
                yield LimitHit(
                    error_type=LimitStatus.UNKNOWN,
                    message=str(e),
                )

    def check_limits(self) -> LimitStatus:
        if not self.is_configured():
            return LimitStatus.NO_KEY
        return self._last_limit_status if self._last_limit_status != LimitStatus.UNKNOWN else LimitStatus.OK
