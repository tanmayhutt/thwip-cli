"""
Groq agent adapter.

High speed LPU inference for Llama 3.3, Mixtral, and Gemma models.
"""

from __future__ import annotations

import os
import shutil
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
    TokenUsage,
    ToolUseStart,
)


class GroqAgent(BaseAgent):
    """
    Groq LPU Inference Agent.

    Ultra-fast inference for rapid iteration.
    """

    name = "groq"
    display_name = "Groq"
    company = "Groq"
    description = "Ultra high-speed LPU inference engine"
    website = "https://groq.com"

    capabilities = {
        Capability.CHAT,
        Capability.FILE_EDIT,
        Capability.FILE_READ,
        Capability.CODE_RUN,
    }

    available_models = [
        ModelInfo(
            id="llama-3.3-70b-versatile",
            name="Llama 3.3 70B (Versatile)",
            context_window=128_000,
            max_output=32_768,
            supports_tools=True,
            supports_streaming=True,
            is_default=True,
            pricing_input=0.59,
            pricing_output=0.79,
        ),
        ModelInfo(
            id="mixtral-8x7b-32768",
            name="Mixtral 8x7B",
            context_window=32_768,
            max_output=32_768,
            supports_tools=True,
            supports_streaming=True,
            pricing_input=0.24,
            pricing_output=0.24,
        ),
    ]

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = None
        self._last_limit_status = LimitStatus.UNKNOWN

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        return os.environ.get("GROQ_API_KEY", "").strip() or None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                key = self._get_api_key()
                self._client = AsyncOpenAI(
                    api_key=key or "dummy",
                    base_url="https://api.groq.com/openai/v1",
                )
            except ImportError:
                raise RuntimeError("openai package required for Groq adapter.")
        return self._client

    def is_installed(self) -> bool:
        return self.is_configured() or shutil.which("groq") is not None

    def is_configured(self) -> bool:
        return bool(self._get_api_key())

    def get_install_info(self) -> dict[str, str]:
        path = shutil.which("groq") or ""
        return {
            "method": "API / CLI" if path else "API Key Configured",
            "path": path,
            "version": "API v1",
        }

    def get_subscription_info(self) -> SubscriptionInfo:
        key = self._get_api_key()
        if not key:
            return SubscriptionInfo(tier=SubscriptionTier.UNKNOWN, is_active=False, message="No API key found")
        return SubscriptionInfo(tier=SubscriptionTier.FREE, is_active=True, message="Groq API key ready")

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
