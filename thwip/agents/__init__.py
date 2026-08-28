"""
Agent registry and discovery for thwip.
"""

from __future__ import annotations

from thwip.agents.base import BaseAgent
from thwip.agents.claude_agent import ClaudeAgent
from thwip.agents.deepseek_agent import DeepSeekAgent
from thwip.agents.google_agent import GoogleAgent
from thwip.agents.groq_agent import GroqAgent
from thwip.agents.ollama_agent import OllamaAgent
from thwip.agents.openai_agent import OpenAIAgent
from thwip.agents.openrouter_agent import OpenRouterAgent
from thwip.config import ThwipConfig

# List of all available agent classes in order
ALL_AGENT_CLASSES: list[type[BaseAgent]] = [
    ClaudeAgent,
    GoogleAgent,
    OpenAIAgent,
    DeepSeekAgent,
    GroqAgent,
    OllamaAgent,
    OpenRouterAgent,
]


class AgentRegistry:
    """Manages instantiated agents and dynamic lookup."""

    def __init__(self, config: ThwipConfig | None = None) -> None:
        self.config = config or ThwipConfig.load()
        self._agents: dict[str, BaseAgent] = {}
        self._initialize_agents()

    def _initialize_agents(self) -> None:
        """Instantiate all agent adapters with configured keys."""
        for agent_cls in ALL_AGENT_CLASSES:
            name = agent_cls.name
            key = self.config.get_key(name)
            if name == "google" and not key:
                key = self.config.get_key("gemini")
            if name == "claude" and not key:
                key = self.config.get_key("anthropic")
            if name == "openai" and not key:
                key = self.config.get_key("codex")

            if agent_cls is OllamaAgent:
                inst = OllamaAgent(host=self.config.ollama_host)
            else:
                inst = agent_cls(api_key=key)

            self._agents[name] = inst

    def get_agent(self, name: str) -> BaseAgent | None:
        """Get agent by name or alias."""
        alias_map = {
            "claude": "claude",
            "claude-code": "claude",
            "anthropic": "claude",
            "google": "google",
            "gemini": "google",
            "antigravity": "google",
            "agy": "google",
            "openai": "openai",
            "codex": "openai",
            "gpt": "openai",
            "chatgpt": "openai",
            "deepseek": "deepseek",
            "groq": "groq",
            "ollama": "ollama",
            "local": "ollama",
            "openrouter": "openrouter",
        }
        normalized = alias_map.get(name.lower().strip(), name.lower().strip())
        return self._agents.get(normalized)

    def list_agents(self) -> list[BaseAgent]:
        """Return all instantiated agents."""
        return list(self._agents.values())

    def get_ready_agents(self) -> list[BaseAgent]:
        """Return all agents that are installed and configured."""
        return [
            a for a in self._agents.values()
            if a.is_installed() and a.is_configured()
        ]

    def find_fallback_agent(self, current_agent_name: str) -> BaseAgent | None:
        """Find the next best ready agent for fallback."""
        ready = self.get_ready_agents()
        for a in ready:
            if a.name != current_agent_name:
                return a
        return None
