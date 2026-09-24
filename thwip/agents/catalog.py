"""Live model catalogs fetched from each provider's own list-models endpoint.

Bundled catalogs in the adapters are only an offline fallback. When a provider
key is configured, the list shown and accepted by Thwip comes from the provider.
"""

from __future__ import annotations

import httpx

from thwip.agents.base import ModelInfo

FETCH_TIMEOUT = 15.0
ANTHROPIC_VERSION = "2023-06-01"

# Substrings that mark non-chat models on providers whose list mixes modalities.
_EXCLUDE = {
    "openai": ("embedding", "tts", "whisper", "transcribe", "realtime", "audio", "image", "dall-e", "moderation",
               "search", "instruct", "davinci", "babbage", "computer-use", "sora"),
    "groq": ("whisper", "tts", "guard", "embed", "orpheus", "playai"),
    "google": ("embedding", "aqa", "imagen", "veo", "tts", "image-generation", "audio", "live", "native-audio"),
}
_INCLUDE_PREFIX = {"openai": ("gpt-", "o1", "o3", "o4", "o5", "codex"), "google": ("gemini", "gemma")}


def tier_for(model_id: str) -> str:
    lowered = model_id.lower()
    if any(word in lowered for word in ("pro", "opus", "fable", "-high", "reasoner", "r1")):
        return "flagship"
    if any(word in lowered for word in ("mini", "nano", "lite", "flash-8b", "haiku", "-low", "small", "fast")):
        return "fast"
    return "balanced"


def _wanted(provider: str, model_id: str) -> bool:
    lowered = model_id.lower()
    if any(word in lowered for word in _EXCLUDE.get(provider, ())):
        return False
    prefixes = _INCLUDE_PREFIX.get(provider)
    return not prefixes or lowered.startswith(prefixes)


def _request(provider: str, api_key: str) -> tuple[str, dict, dict]:
    if provider == "openai":
        return "https://api.openai.com/v1/models", {"Authorization": f"Bearer {api_key}"}, {}
    if provider == "claude":
        return "https://api.anthropic.com/v1/models", {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}, {"limit": 100}
    if provider == "google":
        return "https://generativelanguage.googleapis.com/v1beta/models", {}, {"key": api_key, "pageSize": 200}
    if provider == "deepseek":
        return "https://api.deepseek.com/models", {"Authorization": f"Bearer {api_key}"}, {}
    if provider == "groq":
        return "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {api_key}"}, {}
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {api_key}"}, {}
    raise ValueError(f"No live catalog source for provider '{provider}'.")


def supports_live_catalog(provider: str) -> bool:
    return provider in {"openai", "claude", "google", "deepseek", "groq", "openrouter"}


def parse_models(provider: str, payload: dict) -> list[ModelInfo]:
    """Turn a provider list-models payload into ModelInfo entries; chat models only."""
    models: list[ModelInfo] = []
    if provider == "google":
        for item in payload.get("models", []):
            methods = item.get("supportedGenerationMethods", [])
            model_id = str(item.get("name", "")).removeprefix("models/")
            if "generateContent" not in methods or not _wanted(provider, model_id):
                continue
            models.append(ModelInfo(id=model_id, name=item.get("displayName") or model_id, tier=tier_for(model_id),
                                    context_window=int(item.get("inputTokenLimit") or 0),
                                    max_output=int(item.get("outputTokenLimit") or 0),
                                    supports_thinking="thinking" in model_id or model_id.startswith(("gemini-2.5", "gemini-3"))))
        return models
    for item in payload.get("data", []):
        model_id = str(item.get("id", "")).strip()
        if not model_id or not _wanted(provider, model_id):
            continue
        name = item.get("display_name") or item.get("name") or model_id
        context = int(item.get("context_window") or item.get("context_length") or 0)
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        price_in = _per_million(pricing.get("prompt"))
        price_out = _per_million(pricing.get("completion"))
        top = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        models.append(ModelInfo(id=model_id, name=name, tier=tier_for(model_id), context_window=context,
                                max_output=int(top.get("max_completion_tokens") or 0),
                                pricing_input=price_in, pricing_output=price_out,
                                supports_thinking=any(word in model_id.lower() for word in ("reason", "think", "o1", "o3", "o4", "r1"))))
    return models


def _per_million(value) -> float:
    try:
        return round(float(value) * 1_000_000, 4) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


async def fetch_live_models(provider: str, api_key: str, client: httpx.AsyncClient | None = None) -> list[ModelInfo]:
    """Fetch the provider's current model list using the configured key. Raises on failure."""
    url, headers, params = _request(provider, api_key)
    headers = {key: value for key, value in headers.items() if value and value != "Bearer "}
    owned = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    try:
        response = await client.get(url, headers=headers, params=params)
        if response.status_code in (401, 403):
            raise PermissionError(f"The {provider} key was rejected (HTTP {response.status_code}).")
        response.raise_for_status()
        return parse_models(provider, response.json())
    finally:
        if owned:
            await client.aclose()


def merge_catalog(bundled: list[ModelInfo], live: list[ModelInfo]) -> list[ModelInfo]:
    """Use the live list as truth, keeping bundled pricing/tier metadata for IDs that still exist."""
    known = {model.id: model for model in bundled}
    merged: list[ModelInfo] = []
    for model in live:
        base = known.get(model.id)
        if base:
            merged.append(ModelInfo(**{**vars(base), "context_window": model.context_window or base.context_window,
                                       "max_output": model.max_output or base.max_output,
                                       "pricing_input": model.pricing_input or base.pricing_input,
                                       "pricing_output": model.pricing_output or base.pricing_output,
                                       "is_default": False}))
        else:
            merged.append(ModelInfo(**{**vars(model), "is_default": False}))
    if not merged:
        return merged
    # Keep the bundled default when the provider still offers it; otherwise the first known, then the first listed.
    bundled_default = next((model.id for model in bundled if model.is_default), None)
    ids = [model.id for model in merged]
    default_id = bundled_default if bundled_default in ids else next((i for i in ids if i in known), ids[0])
    merged.sort(key=lambda model: (model.id not in known, list(known).index(model.id) if model.id in known else 0, model.id))
    for model in merged:
        model.is_default = model.id == default_id
    return merged
