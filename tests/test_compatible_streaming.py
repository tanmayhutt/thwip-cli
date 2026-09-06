"""Exercise streaming completion and provider error boundaries without network calls."""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from thwip.agents.base import AgentDone, LimitHit, LimitStatus, TextDelta
from thwip.agents.deepseek_agent import DeepSeekAgent
from thwip.agents.groq_agent import GroqAgent
from thwip.agents.openrouter_agent import OpenRouterAgent


@pytest.mark.parametrize("agent_type", [DeepSeekAgent, GroqAgent, OpenRouterAgent])
@pytest.mark.asyncio
async def test_tool_continuation_serializes_arguments_without_mutating_history(agent_type):
    original = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call1", "type": "function", "function": {
            "name": "read_file", "arguments": {"file_path": "note.txt"},
        },
    }], "_native_state": {"other": "private"}}

    async def create(**kwargs):
        sent = kwargs["messages"][0]
        assert "_native_state" not in sent
        assert json.loads(sent["tool_calls"][0]["function"]["arguments"]) == {"file_path": "note.txt"}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))], usage=None)

    agent = agent_type(api_key="test")
    agent._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events = [event async for event in agent.chat([original], stream=False)]
    assert any(isinstance(event, AgentDone) for event in events)
    assert isinstance(original["tool_calls"][0]["function"]["arguments"], dict)
    assert "_native_state" in original


@pytest.mark.parametrize("agent_type", [DeepSeekAgent, GroqAgent, OpenRouterAgent])
@pytest.mark.asyncio
async def test_stream_text_and_usage_only_final_chunk(agent_type):
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content="hello", tool_calls=None))], usage=None)
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=9, completion_tokens=3))

    async def create(**kwargs):
        assert kwargs["stream"] is True
        return chunks()

    agent = agent_type(api_key="test")
    agent._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events = [event async for event in agent.chat([{"role": "user", "content": "hi"}])]
    assert isinstance(events[0], TextDelta) and events[0].content == "hello"
    assert isinstance(events[-1], AgentDone)
    assert events[-1].usage.input_tokens == 9
    assert events[-1].usage.output_tokens == 3
    assert agent.check_limits() == LimitStatus.OK


@pytest.mark.parametrize("agent_type", [DeepSeekAgent, GroqAgent, OpenRouterAgent])
@pytest.mark.parametrize("status", [429, 500])
@pytest.mark.asyncio
async def test_provider_errors_emit_failure_not_success(agent_type, status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
    error_type = openai.RateLimitError if status == 429 else openai.APIStatusError

    async def create(**kwargs):
        raise error_type("simulated failure", response=response, body=None)

    agent = agent_type(api_key="test")
    agent._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events = [event async for event in agent.chat([])]
    assert len(events) == 1 and isinstance(events[0], LimitHit)
    assert events[0].error_type == (LimitStatus.RATE_LIMITED if status == 429 else LimitStatus.UNKNOWN)
