"""Your own providers: any OpenAI-compatible server, local or hosted.

llama.cpp, LM Studio, vLLM, Ollama's /v1 endpoint, text-generation-webui, a company gateway: anything
that speaks the OpenAI chat-completions protocol. Configured under [providers.<name>] in config.toml
or with /providers add. Models are listed live from the server's /models endpoint when it has one,
or taken from the config; any model ID is accepted and the server validates it.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from thwip.agents.base import Capability, LimitStatus, ModelInfo, SubscriptionInfo
from thwip.agents.openai_agent import OpenAIAgent


class CustomAgent(OpenAIAgent):
    """An OpenAI-compatible endpoint under a name you chose."""

    responses_api = False
    capabilities = {Capability.CHAT, Capability.FILE_READ, Capability.FILE_EDIT, Capability.CODE_RUN,
                    Capability.TERMINAL, Capability.GIT}

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(api_key=spec.get("api_key"))
        self.name = name
        self.display_name = spec.get("display_name") or name
        self.company = spec.get("company") or "Custom"
        self.base_url = str(spec["base_url"]).rstrip("/")
        self.api_key_env = spec.get("api_key_env", "")
        self.available_models = [ModelInfo(id=m, name=m, is_default=(i == 0)) for i, m in enumerate(spec.get("models", []))]
        default = spec.get("default_model")
        if default:
            for model in self.available_models:
                model.is_default = model.id == default
            if default not in {m.id for m in self.available_models}:
                self.available_models.insert(0, ModelInfo(id=default, name=default, is_default=True))
        self.discovery_error = ""

    # --- Keys and client ---

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        if self.api_key_env:
            value = os.environ.get(self.api_key_env, "").strip()
            if value:
                return value
        return "local"   # local servers ignore the key; the OpenAI client requires a non-empty one

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._get_api_key(), base_url=self.base_url)
        return self._client

    # --- Detection ---

    def is_installed(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return bool(self.base_url)

    @property
    def auth_method(self) -> str:
        return "custom_endpoint"

    def get_status_display(self):
        if self.discovery_error:
            return (f"Configured; {self.discovery_error}", "status.limited")
        return (f"Ready ({self.base_url})", "status.ready")

    def get_install_info(self) -> dict[str, str]:
        return {"method": "OpenAI-compatible endpoint", "path": self.base_url, "version": ""}

    def get_subscription_info(self) -> SubscriptionInfo:
        return SubscriptionInfo(is_active=True, message=f"Your own endpoint: {self.base_url}")

    def check_limits(self):
        return LimitStatus.OK

    def get_model_info(self, model_id):
        known = super().get_model_info(model_id)
        if known or not isinstance(model_id, str) or not model_id or any(c.isspace() for c in model_id):
            return known
        return ModelInfo(id=model_id, name=model_id)   # the server validates unknown IDs

    def get_default_model(self) -> str:
        for model in self.available_models:
            if model.is_default:
                return model.id
        return self.available_models[0].id if self.available_models else ""

    # --- Live model list ---

    async def refresh_models(self) -> None:
        """Merge the server's /models list over the configured one; keep the configured list on failure."""
        headers = {}
        key = self._get_api_key()
        if key and key != "local":
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base_url}/models", headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            self.discovery_error = f"model list unavailable ({type(exc).__name__}); using the configured list"
            return
        items = payload.get("data", payload) if isinstance(payload, dict) else payload
        live = []
        for item in items if isinstance(items, list) else []:
            model_id = item.get("id") if isinstance(item, dict) else item
            if isinstance(model_id, str) and model_id:
                live.append(model_id)
        if not live:
            self.discovery_error = "the server listed no models; using the configured list"
            return
        self.discovery_error = ""
        default = self.get_default_model()
        configured = {m.id for m in self.available_models}
        merged = list(self.available_models) + [ModelInfo(id=m, name=m) for m in live if m not in configured]
        for model in merged:
            model.is_default = model.id == (default or live[0])
        self.available_models = merged
