"""
DeepSeek agent adapter.
Uses OpenAI-compatible API client pointing to DeepSeek endpoints.
"""

from __future__ import annotations

import json
import os
import shutil
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


class DeepSeekAgent(BaseAgent):
    """
    DeepSeek V4 agent.

    Capabilities: Chat, code generation, reasoning, file editing.
    Uses OpenAI-compatible client connecting to https://api.deepseek.com.
    """

    name = "deepseek"
    display_name = "DeepSeek"
    company = "DeepSeek"
    description = "High-performance reasoning and coding with DeepSeek V4"
    website = "https://deepseek.com"

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
            id="deepseek-v4-pro",
            name="DeepSeek V4 Pro",
            tier="flagship",
            context_window=1_000_000,
            max_output=384_000,
            supports_tools=True,
            supports_streaming=True,
            supports_thinking=True,
            is_default=True,
            pricing_input=0.435,
            pricing_output=0.87,
        ),
        ModelInfo(
            id="deepseek-v4-flash",
            name="DeepSeek V4 Flash",
            tier="fast",
            context_window=1_000_000,
            max_output=384_000,
            supports_tools=True,
            supports_streaming=True,
            supports_thinking=True,
            pricing_input=0.14,
            pricing_output=0.28,
        ),
    ]

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = None
        self._last_limit_status = LimitStatus.UNKNOWN

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if key:
            return key
        config_path = Path.home() / ".config" / "deepseek" / "config.json"
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text())
                return data.get("api_key") or data.get("apiKey")
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def _ensure_client(self) -> Any:
        if self._client is None:
            key = self._get_api_key()
            if not key:
                raise RuntimeError(
                    "DeepSeek API key not found. Set DEEPSEEK_API_KEY or run /key deepseek to configure."
                )
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(
                    api_key=key,
                    base_url="https://api.deepseek.com",
                )
            except ImportError:
                raise RuntimeError("openai package required for DeepSeek adapter.")
        return self._client

    def is_installed(self) -> bool:
        return self.is_configured() or shutil.which("deepseek") is not None

    def is_configured(self) -> bool:
        return bool(self._get_api_key())

    def get_install_info(self) -> dict[str, str]:
        path = shutil.which("deepseek") or ""
        return {
            "method": "API / CLI" if path else "API Key Configured",
            "path": path,
            "version": "API v1",
        }

    def get_subscription_info(self) -> SubscriptionInfo:
        key = self._get_api_key()
        if not key:
            return SubscriptionInfo(tier=SubscriptionTier.UNKNOWN, is_active=False, message="No API key found")
        return SubscriptionInfo(tier=SubscriptionTier.PRO, is_active=True, message="DeepSeek API key ready")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        import openai

        client = self._ensure_client()
        model = model or self.get_default_model()
        if tools:
            stream = False

        api_messages: list[dict[str, Any]] = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            if stream:
                response = await client.chat.completions.create(**kwargs)
                total_usage = TokenUsage()
                async for chunk in response:
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            yield ThinkingDelta(content=reasoning)
                        if delta.content:
                            yield TextDelta(content=delta.content)
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
                msg = response.choices[0].message
                reasoning = getattr(msg, "reasoning_content", None)
                if reasoning:
                    yield ThinkingDelta(content=reasoning)
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
                    )
                )
                self._last_limit_status = LimitStatus.OK

        except openai.RateLimitError as e:
            self._last_limit_status = LimitStatus.RATE_LIMITED
            yield LimitHit(error_type=LimitStatus.RATE_LIMITED, message=str(e))
        except openai.APIStatusError as e:
            err_text = str(e).lower()
            if "insufficient" in err_text or "quota" in err_text or "balance" in err_text:
                self._last_limit_status = LimitStatus.QUOTA_EXHAUSTED
                yield LimitHit(error_type=LimitStatus.QUOTA_EXHAUSTED, message=str(e))
            else:
                yield LimitHit(error_type=LimitStatus.UNKNOWN, message=str(e))

    def check_limits(self) -> LimitStatus:
        if not self.is_configured():
            return LimitStatus.NO_KEY
        return self._last_limit_status if self._last_limit_status != LimitStatus.UNKNOWN else LimitStatus.OK
