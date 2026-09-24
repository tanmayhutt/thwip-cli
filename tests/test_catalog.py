"""Live model catalogs, exercised against recorded provider payload shapes only."""


import httpx
import pytest

from thwip.agents import AgentRegistry
from thwip.agents.base import ModelInfo
from thwip.agents.catalog import fetch_live_models, merge_catalog, parse_models, supports_live_catalog, tier_for
from thwip.agents.openai_agent import OpenAIAgent
from thwip.config import ThwipConfig

PAYLOADS = {
    "openai": {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.7-astra"}, {"id": "text-embedding-3-large"},
                         {"id": "whisper-1"}, {"id": "gpt-4o-realtime-preview"}, {"id": "o4-mini"}]},
    "claude": {"data": [{"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1"}, {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"}]},
    "google": {"models": [
        {"name": "models/gemini-3.7-flash", "displayName": "Gemini 3.7 Flash", "supportedGenerationMethods": ["generateContent"],
         "inputTokenLimit": 1048576, "outputTokenLimit": 65536},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/imagen-4", "supportedGenerationMethods": ["generateContent"]}]},
    "deepseek": {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]},
    "groq": {"data": [{"id": "openai/gpt-oss-120b", "context_window": 131072}, {"id": "whisper-large-v3"}, {"id": "llama-guard-4"}]},
    "openrouter": {"data": [{"id": "anthropic/claude-opus-5", "name": "Anthropic: Claude Opus 5", "context_length": 200000,
                             "pricing": {"prompt": "0.000015", "completion": "0.000075"}, "top_provider": {"max_completion_tokens": 32000}}]},
}


@pytest.mark.parametrize("provider,expected", [
    ("openai", ["gpt-5.6-sol", "gpt-5.7-astra", "o4-mini"]),
    ("claude", ["claude-fable-5-1", "claude-haiku-4-5"]),
    ("google", ["gemini-3.7-flash"]),
    ("deepseek", ["deepseek-chat", "deepseek-reasoner"]),
    ("groq", ["openai/gpt-oss-120b"]),
    ("openrouter", ["anthropic/claude-opus-5"]),
])
def test_parse_keeps_only_chat_models(provider, expected):
    assert [m.id for m in parse_models(provider, PAYLOADS[provider])] == expected


def test_parse_carries_context_and_pricing():
    google = parse_models("google", PAYLOADS["google"])[0]
    assert (google.context_window, google.max_output, google.name) == (1048576, 65536, "Gemini 3.7 Flash")
    router = parse_models("openrouter", PAYLOADS["openrouter"])[0]
    assert (router.pricing_input, router.pricing_output, router.max_output) == (15.0, 75.0, 32000)
    assert tier_for("gemini-3.1-pro-high") == "flagship" and tier_for("gpt-5-mini") == "fast"


@pytest.mark.asyncio
async def test_fetch_sends_provider_auth_and_rejects_bad_keys():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        if "anthropic" in seen["url"]:
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(200, json=PAYLOADS["openai"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_live_models("openai", "sk-test", client)
        assert [m.id for m in models][:2] == ["gpt-5.6-sol", "gpt-5.7-astra"]
        assert seen["headers"]["authorization"] == "Bearer sk-test"
        with pytest.raises(PermissionError, match="rejected"):
            await fetch_live_models("claude", "bad", client)
        assert seen["headers"]["x-api-key"] == "bad" and "anthropic-version" in seen["headers"]
    assert supports_live_catalog("openai") and not supports_live_catalog("ollama")


def test_merge_uses_live_list_as_truth_and_keeps_known_metadata():
    bundled = [ModelInfo(id="gpt-5.6-sol", name="GPT-5.6 Sol", tier="balanced", pricing_input=1.25, pricing_output=10.0, is_default=True),
               ModelInfo(id="gpt-retired", name="Retired")]
    live = [ModelInfo(id="gpt-5.7-astra", name="gpt-5.7-astra"), ModelInfo(id="gpt-5.6-sol", name="gpt-5.6-sol", context_window=400000)]
    merged = merge_catalog(bundled, live)
    assert [m.id for m in merged] == ["gpt-5.6-sol", "gpt-5.7-astra"]
    sol = merged[0]
    assert sol.is_default and sol.pricing_input == 1.25 and sol.context_window == 400000 and sol.name == "GPT-5.6 Sol"
    assert not merged[1].is_default
    # When the bundled default disappears, the first listed model becomes the default.
    assert merge_catalog(bundled, [ModelInfo(id="gpt-new", name="new")])[0].is_default


@pytest.mark.asyncio
async def test_agent_refresh_replaces_bundled_catalog_when_key_is_set(monkeypatch):
    async def fake_fetch(provider, key, client=None):
        assert (provider, key) == ("openai", "sk-live")
        return parse_models("openai", PAYLOADS["openai"])

    monkeypatch.setattr("thwip.agents.catalog.fetch_live_models", fake_fetch)
    agent = OpenAIAgent(api_key="sk-live")
    assert agent.catalog_source == "bundled"
    await agent.refresh_models()
    assert agent.catalog_source == "live" and agent.get_model_info("gpt-5.7-astra") is not None
    assert agent.get_model_info("gpt-5.6-luna") is None, "models the provider no longer lists are dropped"
    assert OpenAIAgent.available_models[0].id == "gpt-5.6-sol", "class-level bundled fallback is untouched"

    unkeyed = OpenAIAgent(api_key=None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    await unkeyed.refresh_models()
    assert unkeyed.catalog_source == "bundled"


@pytest.mark.asyncio
async def test_agent_refresh_failure_keeps_fallback_with_message(monkeypatch):
    async def failing(provider, key, client=None):
        raise httpx.ConnectError("boom sk-" + "x" * 40)

    monkeypatch.setattr("thwip.agents.catalog.fetch_live_models", failing)
    agent = OpenAIAgent(api_key="sk-live")
    await agent.refresh_models()
    assert agent.catalog_source == "bundled" and "bundled fallback" in agent.discovery_error
    assert "xxxx" not in agent.discovery_error and agent.available_models


@pytest.mark.asyncio
async def test_registry_refreshes_keyed_direct_adapters_at_connect(monkeypatch, tmp_path):
    config = ThwipConfig(project=str(tmp_path))
    config.keys = {"deepseek": "sk-deep"}
    registry = AgentRegistry(config)
    monkeypatch.setattr("thwip.agents.native_agent.shutil.which", lambda name: None)
    monkeypatch.setattr("thwip.agents.native_print.shutil.which", lambda name: None)
    calls = []

    async def fake_fetch(provider, key, client=None):
        calls.append(provider)
        return parse_models(provider, PAYLOADS[provider])

    monkeypatch.setattr("thwip.agents.catalog.fetch_live_models", fake_fetch)
    await registry.connect_native_agents(str(tmp_path))
    assert calls == ["deepseek"]
    assert [m.id for m in registry.get_agent("deepseek").available_models] == ["deepseek-chat", "deepseek-reasoner"]
