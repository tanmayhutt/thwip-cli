"""
Unit tests for agent registry and capabilities.
"""

from __future__ import annotations

from thwip.agents import AgentRegistry
from thwip.agents.base import Capability


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

