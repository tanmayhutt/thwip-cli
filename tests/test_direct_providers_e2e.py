"""Direct API adapters exercised end to end against a local fake provider over real HTTP and real SDKs."""

import pytest

from thwip.agents.base import AgentDone, LimitHit, LimitStatus, TextDelta, ToolUseStart
from thwip.agents.claude_agent import ClaudeAgent
from thwip.agents.deepseek_agent import DeepSeekAgent
from thwip.agents.google_agent import GoogleAgent
from thwip.agents.groq_agent import GroqAgent
from thwip.agents.openai_agent import OpenAIAgent
from thwip.agents.openrouter_agent import OpenRouterAgent
from thwip.endpoints import DEFAULT_BASE_URLS, base_url, is_overridden

from .fake_providers import CHAT_MODELS, GEMINI_MODELS, REPLY, FakeProviderServer

ADAPTERS = {"openai": OpenAIAgent, "claude": ClaudeAgent, "google": GoogleAgent,
            "deepseek": DeepSeekAgent, "groq": GroqAgent, "openrouter": OpenRouterAgent}
ENV = {"openai": "THWIP_OPENAI_BASE_URL", "claude": "THWIP_ANTHROPIC_BASE_URL", "google": "THWIP_GOOGLE_BASE_URL",
       "deepseek": "THWIP_DEEPSEEK_BASE_URL", "groq": "THWIP_GROQ_BASE_URL", "openrouter": "THWIP_OPENROUTER_BASE_URL"}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
          "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}}]
ANTHROPIC_TOOLS = [{"name": "read_file", "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}]


@pytest.fixture(scope="module")
def fake():
    with FakeProviderServer() as server:
        yield server


@pytest.fixture(params=list(ADAPTERS))
def agent(request, fake, monkeypatch):
    provider = request.param
    monkeypatch.setenv(ENV[provider], fake.url)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert is_overridden(provider) and base_url(provider).startswith("http://127.0.0.1")
    return provider, ADAPTERS[provider](api_key="test-key-not-real")


def model_for(provider, limited=False):
    names = GEMINI_MODELS if provider == "google" else CHAT_MODELS
    return names[1] if limited else names[0]


async def run(agent, **kwargs):
    events = []
    async for event in agent.chat(**kwargs):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_live_catalog_over_http(agent):
    provider, adapter = agent
    await adapter.refresh_models()
    assert adapter.catalog_source == "live", adapter.discovery_error
    assert [m.id for m in adapter.available_models] == (GEMINI_MODELS if provider == "google" else CHAT_MODELS)


@pytest.mark.asyncio
async def test_streamed_text_and_usage(agent):
    provider, adapter = agent
    events = await run(adapter, messages=[{"role": "user", "content": "hello"}], model=model_for(provider), system_prompt="Be brief.", stream=True)
    text = "".join(e.content for e in events if isinstance(e, TextDelta))
    assert text == REPLY, events
    done = events[-1]
    assert isinstance(done, AgentDone) and done.usage.input_tokens == 11 and done.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_tool_call_round_trip(agent):
    provider, adapter = agent
    tools = ANTHROPIC_TOOLS if provider == "claude" else TOOLS
    events = await run(adapter, messages=[{"role": "user", "content": "Please read the file note.txt"}], model=model_for(provider),
                       tools=tools, stream=False)
    calls = [e for e in events if isinstance(e, ToolUseStart)]
    assert len(calls) == 1 and calls[0].tool_name == "read_file" and calls[0].args == {"file_path": "note.txt"}, events
    assert isinstance(events[-1], AgentDone)
    # Second round: tool result goes back and the model answers with text.
    follow_up = [{"role": "user", "content": "Please read the file note.txt"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": calls[0].tool_id, "type": "function",
                  "function": {"name": "read_file", "arguments": {"file_path": "note.txt"}}}],
                  **({"_native_state": events[-1].native_state} if events[-1].native_state else {})},
                 {"role": "tool", "tool_call_id": calls[0].tool_id, "name": "read_file", "content": "hello from note"}]
    events = await run(adapter, messages=follow_up, model=model_for(provider), tools=tools, stream=False)
    assert "".join(e.content for e in events if isinstance(e, TextDelta)) == REPLY, events


@pytest.mark.asyncio
async def test_http_429_becomes_limit_hit(agent):
    provider, adapter = agent
    events = await run(adapter, messages=[{"role": "user", "content": "hello"}], model=model_for(provider, limited=True), stream=True)
    assert len(events) == 1 and isinstance(events[0], LimitHit), events
    assert events[0].error_type in {LimitStatus.RATE_LIMITED, LimitStatus.QUOTA_EXHAUSTED}


def test_default_urls_are_used_without_override(monkeypatch):
    for env in ENV.values():
        monkeypatch.delenv(env, raising=False)
    from thwip import endpoints
    endpoints.configure({})
    assert all(base_url(p) == DEFAULT_BASE_URLS[p] for p in ENV)
    endpoints.configure({"openai": "https://proxy.example/v1/"})
    assert base_url("openai") == "https://proxy.example/v1" and is_overridden("openai")
    endpoints.configure({})


@pytest.fixture
def ollama(fake):
    from thwip.agents.ollama_agent import OllamaAgent
    return OllamaAgent(host=fake.url)


def test_ollama_lists_models_from_server(ollama):
    assert ollama.is_configured()
    assert [m.id for m in ollama.available_models] == ["fake-local:latest", "fake-broken:latest"]


@pytest.mark.asyncio
async def test_ollama_stream_tool_round_and_errors(ollama):
    events = await run(ollama, messages=[{"role": "user", "content": "hello"}], model="fake-local:latest", stream=True)
    assert "".join(e.content for e in events if isinstance(e, TextDelta)) == REPLY
    assert isinstance(events[-1], AgentDone) and events[-1].usage.input_tokens == 11
    events = await run(ollama, messages=[{"role": "user", "content": "Please read the file note.txt"}], model="fake-local:latest",
                       tools=TOOLS, stream=False)
    calls = [e for e in events if isinstance(e, ToolUseStart)]
    assert calls and calls[0].tool_name == "read_file" and calls[0].args == {"file_path": "note.txt"}
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await run(ollama, messages=[{"role": "user", "content": "hello"}], model="fake-broken:latest", stream=True)
    from thwip.agents.ollama_agent import OllamaAgent
    with pytest.raises(RuntimeError, match="unreachable"):
        await run(OllamaAgent(host="http://127.0.0.1:9"), messages=[{"role": "user", "content": "hello"}], model="x", stream=True)
