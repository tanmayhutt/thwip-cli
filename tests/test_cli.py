"""Regression tests for CLI orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thwip.agents.base import AgentDone, Capability, TextDelta, ToolUseStart
from thwip.cli import SafeFileHistory, ThwipCLI
from thwip.session import Session
from thwip.tools import ToolManager


class FakeUsageTracker:
    def record_usage(self, **kwargs):
        return None

    def record_limit_hit(self, *args):
        return None


class ToolCallingAgent:
    name = "fake"
    display_name = "Fake Agent"
    company = "Test"
    capabilities = {Capability.CHAT, Capability.FILE_EDIT}

    def __init__(self):
        self.calls = []

    def is_configured(self):
        return True

    def has_capability(self, capability):
        return capability in self.capabilities

    def get_capabilities_for_model(self, model_id):
        return set(self.capabilities)

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if len(self.calls) == 1:
            yield ToolUseStart(tool_id="read-1", tool_name="read_file", args={"file_path": "note.txt"})
            yield AgentDone()
        else:
            assert messages[-2]["role"] == "assistant"
            assert messages[-2]["tool_calls"][0]["id"] == "read-1"
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "read-1"
            assert "known result" in messages[-1]["content"]
            yield TextDelta(content="Used the tool result.")
            yield AgentDone()


@pytest.mark.asyncio
async def test_tool_results_are_returned_to_agent(tmp_path):
    (tmp_path / "note.txt").write_text("known result")
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = SimpleNamespace(stream=True, confirm_tools=True)
    cli.session = Session(current_agent="fake", current_model="fake-model")
    cli.current_agent = ToolCallingAgent()
    cli.tool_manager = ToolManager(tmp_path)
    cli.usage_tracker = FakeUsageTracker()

    await cli.process_user_message("Read the note")

    assert len(cli.current_agent.calls) == 2
    assert cli.session.messages[-1].content == "Used the tool result."
    assert cli.session.observed_tool_results == 1


class UnconfiguredAgent(ToolCallingAgent):
    def is_configured(self):
        return False


@pytest.mark.asyncio
async def test_unconfigured_agent_shows_setup_guidance(tmp_path):
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = SimpleNamespace(stream=True, confirm_tools=True)
    cli.session = Session(current_agent="fake", current_model="fake-model")
    cli.current_agent = UnconfiguredAgent()
    cli.tool_manager = ToolManager(tmp_path)
    cli.usage_tracker = FakeUsageTracker()

    await cli.process_user_message("Hello")

    # Should not invoke chat and should not leave unhandled user message
    assert len(cli.current_agent.calls) == 0
    assert len(cli.session.messages) == 0


def test_inline_api_key_is_rejected():
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = SimpleNamespace(keys={}, key_sources={}, save=lambda: None)

    cli.cmd_auth_config("openai", "secret-value")

    assert cli.config.keys == {}
    assert cli.config.key_sources == {}


def test_inline_api_key_is_not_stored_in_prompt_history(tmp_path):
    history_path = tmp_path / "history.txt"
    history = SafeFileHistory(str(history_path))

    history.store_string("/key openai secret-value")
    history.store_string("/key openai")

    contents = history_path.read_text()
    assert "secret-value" not in contents
    assert "/key openai" in contents
