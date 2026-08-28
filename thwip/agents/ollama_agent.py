"""
Ollama agent adapter.

Local offline models with unlimited quota, zero cost, and full privacy.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from typing import Any

import httpx

from thwip.agents.base import (
    AgentDone,
    AgentEvent,
    BaseAgent,
    Capability,
    LimitStatus,
    ModelInfo,
    SubscriptionInfo,
    SubscriptionTier,
    TextDelta,
    TokenUsage,
    ToolUseStart,
)


class OllamaAgent(BaseAgent):
    """
    Ollama Local Agent.

    Runs entirely on-device with zero rate limits and unlimited usage.
    """

    name = "ollama"
    display_name = "Ollama (Local)"
    company = "Ollama"
    description = "Local models running entirely on-device (zero API limits, private)"
    website = "https://ollama.com"

    capabilities = {
        Capability.CHAT,
        Capability.FILE_EDIT,
        Capability.FILE_READ,
        Capability.CODE_RUN,
        Capability.TERMINAL,
        Capability.GIT,
    }

    def __init__(self, host: str = "http://localhost:11434") -> None:
        self.host = host.rstrip("/")
        self._cached_models: list[ModelInfo] | None = None

    @property
    def available_models(self) -> list[ModelInfo]:
        if self._cached_models is not None:
            return self._cached_models

        # Try to query running Ollama server for downloaded models
        models = []
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    name = m.get("name", "")
                    models.append(
                        ModelInfo(
                            id=name,
                            name=f"{name} (Local)",
                            context_window=32_768,
                            max_output=8_192,
                            supports_tools=True,
                            supports_streaming=True,
                            pricing_input=0.0,
                            pricing_output=0.0,
                        )
                    )
        except Exception:
            pass

        if not models:
            # Defaults
            models = [
                ModelInfo(id="llama3.3", name="Llama 3.3", is_default=True),
                ModelInfo(id="qwen2.5-coder", name="Qwen 2.5 Coder"),
                ModelInfo(id="deepseek-r1", name="DeepSeek R1 Distill"),
                ModelInfo(id="codellama", name="CodeLlama"),
            ]
        self._cached_models = models
        return models

    def is_installed(self) -> bool:
        return shutil.which("ollama") is not None or self._is_server_reachable()

    def _is_server_reachable(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.host}/api/tags")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def is_configured(self) -> bool:
        return self._is_server_reachable()

    def get_install_info(self) -> dict[str, str]:
        path = shutil.which("ollama") or ""
        return {
            "method": "CLI / Local Daemon" if path else "Local Server",
            "path": path or self.host,
            "version": "Local",
        }

    def get_subscription_info(self) -> SubscriptionInfo:
        return SubscriptionInfo(
            tier=SubscriptionTier.UNLIMITED,
            is_active=self.is_configured(),
            message="Unlimited offline local compute",
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[AgentEvent]:
        model = model or self.get_default_model()
        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        formatted_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=120.0) as client:
            if stream:
                async with client.stream(
                    "POST", f"{self.host}/api/chat", json=payload
                ) as resp:
                    if resp.status_code != 200:
                        yield AgentDone()
                        return
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            msg = data.get("message", {})
                            content = msg.get("content", "")
                            if content:
                                yield TextDelta(content=content)
                            for call in msg.get("tool_calls", []):
                                function = call.get("function", {})
                                yield ToolUseStart(
                                    tool_id=call.get("id", function.get("name", "tool")),
                                    tool_name=function.get("name", ""),
                                    args=function.get("arguments", {}),
                                )
                            if data.get("done"):
                                prompt_eval = data.get("prompt_eval_count", 0)
                                eval_count = data.get("eval_count", 0)
                                yield AgentDone(
                                    usage=TokenUsage(
                                        input_tokens=prompt_eval,
                                        output_tokens=eval_count,
                                    )
                                )
                        except json.JSONDecodeError:
                            continue
            else:
                resp = await client.post(f"{self.host}/api/chat", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    message = data.get("message", {})
                    if message.get("content"):
                        yield TextDelta(content=message["content"])
                    for call in message.get("tool_calls", []):
                        function = call.get("function", {})
                        yield ToolUseStart(
                            tool_id=call.get("id", function.get("name", "tool")),
                            tool_name=function.get("name", ""),
                            args=function.get("arguments", {}),
                        )
                    yield AgentDone()

    def check_limits(self) -> LimitStatus:
        return LimitStatus.OK if self.is_configured() else LimitStatus.NO_KEY
