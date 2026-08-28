"""
OpenRouter meta-agent adapter.

Access to over 100+ models from multiple companies through a single unified key.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
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
    TokenUsage,
    ToolUseStart,
)


class OpenRouterAgent(BaseAgent):
    """
    OpenRouter Unified Gateway Agent.

    Routes to any model from Anthropic, OpenAI, Meta, Google, Mistral, etc.
    """

    name = "openrouter"
    display_name = "OpenRouter"
    company = "OpenRouter"
    description = "Universal LLM gateway routing to 100+ models"
    website = "https://openrouter.ai"

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
            id="anthropic/claude-opus-5",
            name="Claude Opus 5 (via OpenRouter)",
            tier="flagship",
            context_window=1_000_000,
            max_output=128_000,
            supports_tools=True,
            supports_thinking=True,
            is_default=True,
            pricing_input=5.0,
            pricing_output=25.0,
        ),
        ModelInfo(
            id="openai/gpt-5.6-terra",
            name="GPT-5.6 Terra (via OpenRouter)",
            tier="balanced",
            context_window=1_050_000,
            supports_tools=True,
            supports_thinking=True,
            pricing_input=2.0,
            pricing_output=12.0,
        ),
        ModelInfo(
            id="deepseek/deepseek-r1",
            name="DeepSeek R1 (via OpenRouter)",
            context_window=64_000,
            pricing_input=0.55,
            pricing_output=2.19,
        ),
        ModelInfo(
            id="google/gemini-3.7-flash",
            name="Gemini 3.7 Flash (via OpenRouter)",
            tier="fast",
            context_window=1_048_576,
            max_output=65_536,
            supports_tools=True,
            supports_thinking=True,
            pricing_input=0.375,
            pricing_output=1.875,
        ),
    ]

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = None
        self._last_limit_status = LimitStatus.UNKNOWN

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        return os.environ.get("OPENROUTER_API_KEY", "").strip() or None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                key = self._get_api_key()
                self._client = AsyncOpenAI(
                    api_key=key or "dummy",
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={
                        "HTTP-Referer": "https://github.com/tanmayhutt/thwip-cli",
                        "X-Title": "thwip",
                    },
                )
            except ImportError:
                raise RuntimeError("openai package required for OpenRouter adapter.")
        return self._client

    def is_installed(self) -> bool:
        return self.is_configured()

    def is_configured(self) -> bool:
        return bool(self._get_api_key())

    def get_install_info(self) -> dict[str, str]:
        return {
            "method": "API Gateway",
            "path": "https://openrouter.ai",
            "version": "v1",
        }

    def get_subscription_info(self) -> SubscriptionInfo:
        key = self._get_api_key()
        return SubscriptionInfo(
            tier=SubscriptionTier.PRO if key else SubscriptionTier.UNKNOWN,
            is_active=bool(key),
            message="OpenRouter account ready" if key else "No API key",
        )

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
                        if delta.content:
                            yield TextDelta(content=delta.content)
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
        except Exception as e:
            yield LimitHit(error_type=LimitStatus.UNKNOWN, message=str(e))

    def check_limits(self) -> LimitStatus:
        if not self.is_configured():
            return LimitStatus.NO_KEY
        return self._last_limit_status if self._last_limit_status != LimitStatus.UNKNOWN else LimitStatus.OK
