"""
Google / Antigravity agent adapter.

Full capabilities: chat, file editing, code execution, terminal, browser, search.
Detects Antigravity IDE, Gemini CLI, and Google API keys.
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


class GoogleAgent(BaseAgent):
    """
    Google Antigravity / Gemini: full coding agent.
    Capabilities: Chat, file editing, code execution, terminal, browser, web search.
    Uses the google-genai SDK for API communication.
    """

    name = "google"
    display_name = "Antigravity / Gemini"
    company = "Google"
    description = "Google's agentic coding assistant with full IDE, terminal, and browser access"
    website = "https://gemini.google.com"

    capabilities = {
        Capability.CHAT,
        Capability.FILE_EDIT,
        Capability.FILE_READ,
        Capability.CODE_RUN,
        Capability.TERMINAL,
        Capability.BROWSER,
        Capability.SEARCH,
        Capability.IMAGE_GEN,
    }

    available_models = [
        ModelInfo(
            id="gemini-2.5-pro",
            name="Gemini 2.5 Pro",
            context_window=1_048_576,
            max_output=65_536,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            is_default=True,
            pricing_input=1.25,
            pricing_output=10.0,
        ),
        ModelInfo(
            id="gemini-2.5-flash",
            name="Gemini 2.5 Flash",
            context_window=1_048_576,
            max_output=65_536,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            pricing_input=0.15,
            pricing_output=0.60,
        ),
    ]

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = None
        self._last_limit_status = LimitStatus.UNKNOWN

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key

        # 1. Environment variables
        for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            key = os.environ.get(env_var, "").strip()
            if key:
                return key

        # 2. Gemini CLI / Antigravity config files
        config_paths = [
            Path.home() / ".config" / "gemini" / "config.json",
            Path.home() / ".gemini" / "config.json",
            Path.home() / ".config" / "antigravity" / "config.json",
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
                from google import genai
                key = self._get_api_key()
                if key:
                    self._client = genai.Client(api_key=key)
                else:
                    self._client = genai.Client()
            except ImportError:
                raise RuntimeError(
                    "google-genai package not installed. Run: pip install google-genai"
                )
        return self._client

    # --- Detection ---

    def is_installed(self) -> bool:
        """Check if Gemini CLI, Antigravity IDE, or Gemini app is installed."""
        if self.is_configured():
            return True
        if any(shutil.which(cmd) is not None for cmd in ("gemini", "agy", "antigravity")):
            return True
        app_paths = [
            Path("/Applications/Antigravity IDE.app"),
            Path("/Applications/Antigravity.app"),
            Path("/Applications/Gemini.app"),
            Path.home() / "Applications" / "Antigravity IDE.app",
            Path.home() / "Applications" / "Antigravity.app",
            Path.home() / ".gemini",
        ]
        return any(p.exists() for p in app_paths)

    def is_configured(self) -> bool:
        return bool(self._get_api_key())

    def get_install_info(self) -> dict[str, str]:
        info: dict[str, str] = {"method": "not installed", "path": "", "version": ""}

        for cmd in ("gemini", "agy", "antigravity"):
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

        # Also check for Antigravity IDE or Gemini app (macOS app)
        if not info["path"]:
            app_paths = [
                Path("/Applications/Antigravity IDE.app"),
                Path("/Applications/Antigravity.app"),
                Path("/Applications/Gemini.app"),
                Path.home() / "Applications" / "Antigravity IDE.app",
                Path.home() / "Applications" / "Antigravity.app",
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
        if not key:
            return SubscriptionInfo(
                tier=SubscriptionTier.UNKNOWN,
                is_active=False,
                message="No API key found",
            )

        # Google Gemini API keys typically start with "AIza"
        if key.startswith("AIza"):
            return SubscriptionInfo(
                tier=SubscriptionTier.FREE,  # Free tier by default
                is_active=True,
                message="Google API key detected",
            )

        return SubscriptionInfo(
            tier=SubscriptionTier.UNKNOWN,
            is_active=True,
            message="API key detected",
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
        """Stream a chat response from Gemini."""
        client = self._ensure_client()
        model = model or self.get_default_model()

        # Convert messages to Gemini format
        gemini_contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            gemini_contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}],
            })

        config: dict[str, Any] = {}
        if system_prompt:
            config["system_instruction"] = system_prompt

        model_info = self.get_model_info(model)
        if model_info and model_info.supports_thinking:
            config["thinking_config"] = {"thinking_budget": 8192}

        try:
            from google.genai import types

            if stream:
                response = client.models.generate_content_stream(
                    model=model,
                    contents=gemini_contents,
                    config=types.GenerateContentConfig(**config) if config else None,
                )

                total_input = 0
                total_output = 0

                for chunk in response:
                    if chunk.candidates:
                        for candidate in chunk.candidates:
                            if candidate.content and candidate.content.parts:
                                for part in candidate.content.parts:
                                    if hasattr(part, "thought") and part.thought:
                                        yield ThinkingDelta(content=part.text or "")
                                    elif hasattr(part, "text") and part.text:
                                        yield TextDelta(content=part.text)

                    if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                        um = chunk.usage_metadata
                        total_input = getattr(um, "prompt_token_count", 0) or 0
                        total_output = getattr(um, "candidates_token_count", 0) or 0

                yield AgentDone(
                    usage=TokenUsage(
                        input_tokens=total_input,
                        output_tokens=total_output,
                    )
                )
                self._last_limit_status = LimitStatus.OK
            else:
                response = client.models.generate_content(
                    model=model,
                    contents=gemini_contents,
                    config=types.GenerateContentConfig(**config) if config else None,
                )

                if response.candidates:
                    for candidate in response.candidates:
                        if candidate.content and candidate.content.parts:
                            for part in candidate.content.parts:
                                if hasattr(part, "text") and part.text:
                                    yield TextDelta(content=part.text)

                um = getattr(response, "usage_metadata", None)
                yield AgentDone(
                    usage=TokenUsage(
                        input_tokens=getattr(um, "prompt_token_count", 0) or 0,
                        output_tokens=getattr(um, "candidates_token_count", 0) or 0,
                    )
                )
                self._last_limit_status = LimitStatus.OK

        except Exception as e:
            error_str = str(e).lower()
            if "resource" in error_str and "exhaust" in error_str:
                self._last_limit_status = LimitStatus.RATE_LIMITED
                yield LimitHit(
                    error_type=LimitStatus.RATE_LIMITED,
                    message=str(e),
                )
            elif "quota" in error_str or "limit" in error_str:
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
