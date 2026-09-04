"""Offline regression coverage for auditable text handoffs."""

import importlib
import json
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from rich.console import Console

from thwip.agents.base import Capability, ModelInfo
from thwip.cli import ThwipCLI
from thwip.handoff import build_handoff_report
from thwip.session import Message, Session
from thwip.shortcuts import ThwipCompleter
from thwip.tools import ToolManager


class OfflineAgent:
    name = "offline"
    company = "Test"
    display_name = "Offline Agent"
    capabilities = {Capability.CHAT, Capability.FILE_EDIT}

    def __init__(self, window=100_000, tools=True):
        self.model = ModelInfo(id="test-model", name="Test", context_window=window,
                               max_output=4096, supports_tools=tools)
        self.available_models = [self.model]

    def get_model_info(self, model):
        return self.model if model == self.model.id else None

    def get_default_model(self):
        return self.model.id

    def get_handoff_models(self):
        return self.available_models

    def get_capabilities_for_model(self, model):
        return {Capability.CHAT, Capability.FILE_EDIT} if self.model.supports_tools else {Capability.CHAT}

    def is_installed(self):
        return True

    def is_configured(self):
        return False


def test_fingerprint_is_stable_and_does_not_mutate_session():
    session = Session(current_model="test-model")
    session.add_user_message("Keep Unicode: नमस्ते")
    session.add_assistant_message("Understood", "offline", "test-model")
    before = deepcopy(session)
    agent = OfflineAgent()
    first = build_handoff_report(session, agent, agent, "test-model")
    second = build_handoff_report(session, agent, agent, "test-model", [{"name": "read"}])
    assert first.text_fingerprint == second.text_fingerprint
    assert second.estimated_input_tokens > first.estimated_input_tokens
    assert session == before
    assert first.transferred_messages == 2
    session.system_prompt += "Changed instruction"
    assert build_handoff_report(session, agent, agent, "test-model").text_fingerprint != first.text_fingerprint


@pytest.mark.parametrize("change", ["text", "order"])
def test_fingerprint_changes_with_payload(change):
    session = Session()
    session.add_user_message("one")
    session.add_user_message("two")
    agent = OfflineAgent()
    initial = build_handoff_report(session, agent, agent, "test-model")
    if change == "text":
        session.messages[0].content = "different"
    else:
        session.messages.reverse()
    assert build_handoff_report(session, agent, agent, "test-model").text_fingerprint != initial.text_fingerprint


def test_exclusions_and_capability_changes_are_explicit():
    session = Session(current_model="test-model")
    session.messages = [Message(role="assistant", content="text", tool_calls=[{"id": "one"}]),
                        Message(role="tool", content="private result")]
    session.record_tool_result()
    source, target = OfflineAgent(), OfflineAgent(tools=False)
    report = build_handoff_report(session, source, target, "test-model")
    assert report.transferred_messages == 1
    assert report.excluded_messages == report.excluded_tool_calls == report.observed_tool_results == 1
    assert report.lost_capabilities == (Capability.FILE_EDIT.display_name,)
    assert report.gained_capabilities == ()
    reverse = build_handoff_report(session, target, source, "test-model")
    assert reverse.gained_capabilities == report.lost_capabilities
    assert "private result" not in repr(report)


@pytest.mark.parametrize("window,expected", [(0, "unknown"), (100, "likely over budget"),
                                             (5000, "near limit"), (100000, "below advisory budget")])
def test_advisory_pressure(window, expected):
    agent = OfflineAgent(window=window)
    report = build_handoff_report(Session(), agent, agent, "test-model")
    assert report.context_pressure == expected


def test_unknown_model_is_not_silently_assumed():
    with pytest.raises(ValueError, match="Unknown model"):
        build_handoff_report(Session(), OfflineAgent(), OfflineAgent(), "missing")


def test_tracking_survives_save_and_legacy_load(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    session = Session(name="tracking")
    session.record_tool_result()
    path = session.save()
    loaded = Session.load("tracking")
    assert loaded.observed_tool_results == 1
    assert loaded.tool_tracking_complete
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    del data["observed_tool_results"]
    del data["tool_tracking_complete"]
    path.write_text(json.dumps(data))
    legacy = Session.load("tracking")
    assert not legacy.tool_tracking_complete
    legacy.record_tool_result()
    assert legacy.observed_tool_results == 1
    legacy.clear_context()
    assert legacy.observed_tool_results == 0
    assert legacy.tool_tracking_complete


@pytest.mark.asyncio
async def test_cli_preview_is_offline_non_mutating_and_safe_to_render(tmp_path, monkeypatch):
    agent = OfflineAgent()
    agent.name = "[red]offline[/red]"
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.current_agent = agent
    cli.registry = SimpleNamespace(get_agent=lambda name: agent if name == "offline" else None)
    cli.session = Session(current_model="test-model")
    cli.session.add_user_message("PRIVATE USER TEXT")
    cli.tool_manager = ToolManager(tmp_path)
    buffer = StringIO()
    monkeypatch.setattr("thwip.cli.console", Console(file=buffer, width=140, color_system=None))
    before = deepcopy(cli.session)
    await cli.handle_command("/handoff offline test-model")
    assert cli.session == before
    assert "PRIVATE USER TEXT" not in buffer.getvalue()
    assert "[red]offline[/red]" in buffer.getvalue()
    assert "No model calls" in " ".join(buffer.getvalue().split())


@pytest.mark.asyncio
async def test_switch_previews_before_mutating(tmp_path, monkeypatch):
    source, target = OfflineAgent(), OfflineAgent(tools=False)
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.current_agent = source
    cli.registry = SimpleNamespace(get_agent=lambda name: target)
    cli.session = Session(current_model="test-model")
    cli.tool_manager = ToolManager(tmp_path)
    calls = []

    def preview(*args):
        assert cli.current_agent is source
        calls.append(args)

    monkeypatch.setattr(cli, "cmd_handoff", preview)
    await cli.cmd_switch("offline", "test-model")
    assert calls == [("offline", "test-model")]
    assert cli.current_agent is target


@pytest.mark.parametrize("command", ["/clear", "/session clear"])
@pytest.mark.asyncio
async def test_clear_commands_reset_tracking(command):
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.session = Session(observed_tool_results=2, tool_tracking_complete=False)
    await cli.handle_command(command)
    assert cli.session.observed_tool_results == 0
    assert cli.session.tool_tracking_complete


def test_handoff_completion():
    matches = list(ThwipCompleter(["claude"]).get_completions(Document("/handoff cl"), None))
    assert [match.text for match in matches] == ["/handoff claude"]


def test_ollama_preview_never_discovers_models_over_network(monkeypatch):
    from thwip.agents.ollama_agent import OllamaAgent

    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not access the network")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    agent = OllamaAgent(host="https://example.invalid")
    session = Session(current_agent="ollama", current_model="llama3.3")
    report = build_handoff_report(session, agent, agent, "llama3.3")
    assert report.context_pressure == "unknown"
    assert agent._cached_models is None


@pytest.mark.parametrize("module,class_name", [
    ("claude", "ClaudeAgent"), ("google", "GoogleAgent"), ("openai", "OpenAIAgent"),
    ("deepseek", "DeepSeekAgent"), ("groq", "GroqAgent"), ("ollama", "OllamaAgent"),
    ("openrouter", "OpenRouterAgent"),
])
def test_every_adapter_supports_offline_catalog_reports(module, class_name, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Handoff must not discover providers or generate responses")

    adapter = getattr(importlib.import_module(f"thwip.agents.{module}_agent"), class_name)
    agent = adapter.__new__(adapter)
    if module == "ollama":
        agent._cached_models = None
    monkeypatch.setattr(agent, "chat", forbidden)
    monkeypatch.setattr(agent, "is_installed", forbidden)
    monkeypatch.setattr(agent, "is_configured", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    model = agent.get_handoff_models()[0]
    session = Session(current_agent=agent.name, current_model=model.id)
    report = build_handoff_report(session, agent, agent, model.id)
    assert report.lost_capabilities == report.gained_capabilities == ()
    assert len(report.text_fingerprint) == 64
