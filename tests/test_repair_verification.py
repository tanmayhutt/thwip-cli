"""Focused verification of repaired settings and native continuation."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from rich.text import Text

from thwip.agents.base import AgentDone
from thwip.agents.claude_agent import ClaudeAgent
from thwip.agents.google_agent import GoogleAgent
from thwip.agents.openai_agent import OpenAIAgent
from thwip.cli import ThwipCLI
from thwip.config import ThwipConfig
from thwip.detector import SystemDetector
from thwip.limits import UsageTracker
from thwip.session import Session
from thwip.tools.terminal import TerminalRunner


@pytest.mark.parametrize("field,value", [
    ("estimated_cost", float("nan")), ("estimated_cost", float("inf")),
    ("estimated_cost", True), ("estimated_cost", 10 ** 400),
    ("last_limit_hit_timestamp", -1), ("last_limit_hit_timestamp", "bad"),
    ("last_limit_hit_timestamp", float("nan")), ("last_error", []),
])
def test_usage_rejects_corrupt_values_without_losing_other_agents(tmp_path, monkeypatch, field, value):
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"bad": {field: value}, "good": {"estimated_cost": 0.5}}))
    monkeypatch.setattr("thwip.limits.get_usage_path", lambda: path)
    tracker = UsageTracker()
    assert set(tracker.stats) == {"good"}
    assert tracker.get_summary()["total_cost"] == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,auth", [("ollama", "none"), ("openai", "subscription"), ("google", "oauth"), ("claude", "none")])
async def test_setup_guidance_matches_transport(monkeypatch, provider, auth):
    panels = []
    monkeypatch.setattr("thwip.cli.console.print", lambda panel: panels.append(panel))
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.current_agent = SimpleNamespace(
        name=provider, display_name="Provider [literal]", auth_method=auth,
        is_configured=lambda: False,
    )
    cli.session = Session(current_model="model [literal]")
    await cli.process_user_message("hello")
    text = panels[0].renderable.plain
    assert not cli.session.messages
    if provider == "ollama":
        assert "ollama serve" in text and "No API key is required" in text
        assert "/key" not in text
    else:
        assert "model [literal]" in text and f"/key {provider}" in text
        assert ("sign-in was detected" in text) == (auth in ("oauth", "subscription"))


def test_display_switches_hide_status_fields():
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = ThwipConfig()
    cli.current_agent = OpenAIAgent(api_key="test")
    cli.session = Session(current_model=cli.current_agent.get_default_model())
    cli.session.add_assistant_message("done", "openai", cli.session.current_model, tokens=42)
    cli.usage_tracker = SimpleNamespace(get_summary=lambda: {"total_cost": 1.25})
    visible = cli._render_status().plain
    assert "42 tok" in visible and "$1.2500" in visible and "[chat]" in visible
    for name in ("show_agent_badge", "show_token_count", "show_cost", "show_capabilities"):
        setattr(cli.config.display, name, False)
    assert cli._render_status().plain == ""
    cli.config.display.markdown = False
    assert isinstance(cli._render_response("**literal**"), Text)
    assert cli._render_response("**literal**").plain == "**literal**"


@pytest.mark.parametrize("content,expected", [
    ('{"userID":"metadata-only"}', False),
    ('{"apiKey":""}', False),
    ('{"apiKey":42}', False),
    ('{"apiKey":"test-key"}', True),
    ('{"credentials":{"apiKey":"test-key"}}', True),
])
def test_detector_requires_actual_key_field(tmp_path, monkeypatch, content, expected):
    path = tmp_path / "claude.json"
    path.write_text(content)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("thwip.detector.KNOWN_CLI_TOOLS", [{
        "name": "Claude", "company": "Anthropic", "binaries": [], "category": "CLI Agent",
        "key_env": "ANTHROPIC_API_KEY", "config_files": [str(path)], "capabilities": [],
    }])
    monkeypatch.setattr("thwip.detector.KNOWN_APPS", [])
    monkeypatch.setattr("thwip.detector.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1))
    found = SystemDetector().scan_all()
    assert bool(found) is expected


@pytest.mark.asyncio
async def test_openai_preserves_reasoning_and_function_items():
    captured = []
    items = [SimpleNamespace(type="reasoning", id="r1", encrypted_content="opaque", summary=[]),
             SimpleNamespace(type="function_call", call_id="c1", name="read_file", arguments='{"file_path":"x"}')]

    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(output_text="", output=items, usage=None)

    agent = OpenAIAgent(api_key="test")
    agent._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    events = [e async for e in agent.chat(messages=[{"role": "user", "content": "read"}], stream=False)]
    done = next(e for e in events if isinstance(e, AgentDone))
    messages = [{"role": "assistant", "content": "", "_native_state": done.native_state},
                {"role": "tool", "tool_call_id": "c1", "content": "result"}]
    _ = [e async for e in agent.chat(messages=messages, stream=False)]
    assert captured[-1]["input"][:2] == [vars(item) for item in items]
    assert captured[-1]["input"][2]["type"] == "function_call_output"
    assert captured[-1]["store"] is False


@pytest.mark.asyncio
async def test_claude_preserves_thinking_signature():
    captured = []
    blocks = [SimpleNamespace(type="thinking", thinking="thought", signature="signature"),
              SimpleNamespace(type="tool_use", id="c1", name="read_file", input={"file_path":"x"})]

    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_reason="tool_use")

    agent = ClaudeAgent(api_key="test")
    agent._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    events = [e async for e in agent.chat(messages=[], stream=False)]
    done = next(e for e in events if isinstance(e, AgentDone))
    _ = [e async for e in agent.chat(messages=[{"role":"assistant", "_native_state":done.native_state}], stream=False)]
    assert captured[-1]["messages"][0]["content"] == [vars(block) for block in blocks]


@pytest.mark.asyncio
async def test_google_preserves_thought_signature():
    from google.genai import types

    captured = []
    content = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name="read_file", args={"file_path":"x"}),
        thought_signature=b"opaque-signature",
    )])

    def generate(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)], usage_metadata=None)

    agent = GoogleAgent(api_key="test")
    agent._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    events = [e async for e in agent.chat(messages=[], stream=False)]
    done = next(e for e in events if isinstance(e, AgentDone))
    _ = [e async for e in agent.chat(messages=[{"role":"assistant", "_native_state":done.native_state}], stream=False)]
    assert captured[-1]["contents"][0].parts[0].thought_signature == b"opaque-signature"


def test_sync_timeout_stops_child_writes(tmp_path):
    runner = TerminalRunner(tmp_path)
    result = runner.run_command("sleep 0.3; printf unwanted > late.txt", timeout=0.03)
    assert "timed out" in result
    time.sleep(0.4)
    assert not (tmp_path / "late.txt").exists()


@pytest.mark.asyncio
async def test_async_cancellation_stops_child_writes(tmp_path):
    runner = TerminalRunner(tmp_path)
    task = asyncio.create_task(runner.run_command_async("sleep 0.3; printf unwanted > late.txt"))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.4)
    assert not (tmp_path / "late.txt").exists()
