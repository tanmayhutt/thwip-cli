"""
Unit tests for agent registry and capabilities.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thwip.agents import AgentRegistry
from thwip.agents.base import Capability
from thwip.agents.google_agent import GoogleAgent
from thwip.agents.openai_agent import OpenAIAgent


def test_agent_registry_initialization():
    registry = AgentRegistry()
    agents = registry.list_agents()
    assert len(agents) >= 7

    claude = registry.get_agent("claude")
    assert claude is not None
    assert claude.company == "Anthropic"
    assert Capability.FILE_EDIT in claude.capabilities

    google = registry.get_agent("gemini")
    assert google is not None
    assert google.company == "Google"

    openai = registry.get_agent("codex")
    assert openai is not None
    assert openai.company == "OpenAI"

    ollama = registry.get_agent("ollama")
    assert ollama is not None


def test_capability_comparison():
    registry = AgentRegistry()
    claude = registry.get_agent("claude")
    groq = registry.get_agent("groq")

    # Claude has GIT and BROWSER, Groq might not
    missing = groq.get_missing_capabilities(claude.capabilities)
    assert isinstance(missing, list)


def test_model_tool_support_exposes_workspace_capabilities():
    registry = AgentRegistry()
    deepseek = registry.get_agent("deepseek")

    assert Capability.FILE_EDIT in deepseek.get_capabilities_for_model("deepseek-v4-pro")
    assert Capability.FILE_EDIT in deepseek.get_capabilities_for_model("deepseek-v4-flash")


def test_model_tiers():
    registry = AgentRegistry()
    google = registry.get_agent("google")
    assert google is not None

    flagship = [m for m in google.available_models if m.tier == "flagship"]
    balanced = [m for m in google.available_models if m.tier == "balanced"]
    fast = [m for m in google.available_models if m.tier == "fast"]

    assert len(flagship) >= 1
    assert len(balanced) >= 1
    assert len(fast) >= 1


def test_installed_agent_filtering():
    registry = AgentRegistry()
    installed = [a for a in registry.list_agents() if a.is_installed()]
    ready = registry.get_ready_agents()

    assert isinstance(installed, list)
    assert isinstance(ready, list)
    # Every ready agent must be installed
    for a in ready:
        assert a in installed


def test_cli_oauth_is_not_misreported_as_sdk_api_access(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"id_token":"x.y.z"}}')
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    (gemini_dir / "google_accounts.json").write_text('{"active":"user@example.com"}')

    openai = OpenAIAgent()
    google = GoogleAgent()

    assert openai.auth_method == "subscription"
    assert google.auth_method == "oauth"
    assert not openai.is_configured()
    assert not google.is_configured()


@pytest.mark.asyncio
async def test_openai_tools_use_responses_api():
    captured = {}

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text="",
                output=[SimpleNamespace(
                    type="function_call",
                    call_id="call-1",
                    name="read_file",
                    arguments='{"file_path":"README.md"}',
                )],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    agent = OpenAIAgent(api_key="test-key")
    agent._client = SimpleNamespace(responses=FakeResponses())
    events = [
        event
        async for event in agent.chat(
            messages=[{"role": "user", "content": "Read it"}],
            model="gpt-5.6-terra",
            tools=[{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            stream=True,
        )
    ]

    assert captured["model"] == "gpt-5.6-terra"
    assert captured["reasoning"] == {"effort": "medium"}
    assert events[0].tool_name == "read_file"
    assert events[0].args == {"file_path": "README.md"}
