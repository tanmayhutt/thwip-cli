"""Regression tests for CLI orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thwip.agents.base import AgentDone, Capability, TextDelta, ToolUseStart
from thwip.cli import ThwipCLI
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
