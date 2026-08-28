"""
Google / Antigravity agent adapter.

Capabilities: chat plus Thwip's local file, terminal, and Git tools.
Detects Antigravity IDE, Gemini CLI, and Google API keys.
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


class GoogleAgent(BaseAgent):
    """
    Google Antigravity / Gemini coding agent.
    Uses the google-genai SDK for API communication.
    """

    name = "google"
    display_name = "Antigravity / Gemini"
    company = "Google"
    description = "Google's Gemini models with Thwip's local coding tools"
    website = "https://gemini.google.com"

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
            id="gemini-3.1-pro-preview",
            name="Gemini 3.1 Pro Preview",
            tier="flagship",
            description="Advanced multimodal reasoning for complex coding tasks",
            context_window=1_048_576,
            max_output=65_536,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            pricing_input=2.0,
            pricing_output=12.0,
        ),
        ModelInfo(
            id="gemini-3.7-flash",
            name="Gemini 3.7 Flash",
            tier="balanced",
            description="Latest stable Flash model for coding and agentic workflows",
            context_window=1_048_576,
            max_output=65_536,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            is_default=True,
            pricing_input=0.75,
            pricing_output=3.75,
        ),
        ModelInfo(
            id="gemini-3.5-flash-lite",
            name="Gemini 3.5 Flash-Lite",
            tier="fast",
            description="Stable low-cost model for high-volume agentic tasks",
            context_window=1_048_576,
            max_output=65_536,
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_thinking=True,
            pricing_input=0.30,
            pricing_output=2.50,
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

    def _has_cli_auth(self) -> dict[str, str] | None:
        """Check for Google OAuth via Gemini CLI's google_accounts.json."""
        acc_file = Path.home() / ".gemini" / "google_accounts.json"
        if acc_file.is_file():
            try:
                data = json.loads(acc_file.read_text())
                account = data.get("active") or ""
                if not account and data.get("old"):
                    account = data["old"][0]
                if account:
                    return {"method": "google_oauth", "account": account}
            except Exception:
                pass
        return None

    @property
    def auth_method(self) -> str:
        if self._get_api_key():
            return "api_key"
        if self._has_cli_auth():
            return "oauth"
        return "none"

    def is_configured(self) -> bool:
        # Gemini CLI OAuth proves the CLI is signed in, but google-genai cannot
        # consume that private credential store as an API key.
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

        # Check for Antigravity IDE or Gemini app (macOS app)
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

        # Check for Google Account in ~/.gemini/google_accounts.json
        acc_file = Path.home() / ".gemini" / "google_accounts.json"
        if acc_file.is_file():
            try:
                acc_data = json.loads(acc_file.read_text())
                account = acc_data.get("active") or (acc_data.get("old") and acc_data["old"][0]) or ""
                if account:
                    info["account"] = account
            except Exception:
                pass

        return info

    def get_subscription_info(self) -> SubscriptionInfo:
        key = self._get_api_key()
        if key:
            if key.startswith("AIza"):
                return SubscriptionInfo(
                    tier=SubscriptionTier.FREE,
                    is_active=True,
                    message="Google API key detected",
                )
            return SubscriptionInfo(
                tier=SubscriptionTier.UNKNOWN,
                is_active=True,
                message="API key detected",
            )

        cli_auth = self._has_cli_auth()
        if cli_auth:
            account = cli_auth.get("account", "")
            return SubscriptionInfo(
                tier=SubscriptionTier.PRO,
                is_active=True,
                message=f"Google OAuth ({account})" if account else "Google OAuth active",
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

        try:
            from google.genai import types

            if tools:
                declarations = []
                for tool in tools:
                    function = tool.get("function", tool)
                    declarations.append(
                        types.FunctionDeclaration(
                            name=function["name"],
                            description=function.get("description", ""),
                            parameters_json_schema=function.get("parameters", {}),
                        )
                    )
                config["tools"] = [types.Tool(function_declarations=declarations)]

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
                                    elif getattr(part, "function_call", None):
                                        call = part.function_call
                                        yield ToolUseStart(
                                            tool_id=call.id or call.name or "tool",
                                            tool_name=call.name or "",
                                            args=dict(call.args or {}),
                                        )
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
                                if getattr(part, "function_call", None):
                                    call = part.function_call
                                    yield ToolUseStart(
                                        tool_id=call.id or call.name or "tool",
                                        tool_name=call.name or "",
                                        args=dict(call.args or {}),
                                    )
                                elif hasattr(part, "text") and part.text:
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
