"""Print-mode native adapters, exercised with fake CLI processes only."""

import asyncio
import json

import pytest

from thwip.agents import AgentRegistry
from thwip.agents.base import AgentDone, LimitHit, LimitStatus, NativeActivity, TextDelta, ThinkingDelta
from thwip.agents.native_common import build_native_prompt, classify_limit, scrub
from thwip.agents.native_print import PrintAgent
from thwip.config import ThwipConfig


class FakeProcess:
    def __init__(self, lines, returncode=0, stderr=b""):
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data((json.dumps(line) if not isinstance(line, (bytes, str)) else line).encode()
                                  if not isinstance(line, bytes) else line)
            self.stdout.feed_data(b"\n")
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode = None
        self._code = returncode
        self.killed = False
        self.pid = 4242

    async def wait(self):
        self.returncode = self._code
        return self._code


def install_fake(monkeypatch, agent, lines, returncode=0, stderr=b""):
    calls = []

    async def start(command, stdin_text=None):
        calls.append((command, stdin_text))
        return FakeProcess(lines, returncode, stderr)

    monkeypatch.setattr(agent, "_start_process", start)
    monkeypatch.setattr("thwip.agents.native_print.terminate_process_tree", lambda process: setattr(process, "killed", True))
    return calls


async def collect(agent, model="fable"):
    events = []
    async for event in agent.chat([{"role": "user", "content": "hello"}], model=model, system_prompt="Be brief."):
        events.append(event)
    return events


def test_prompt_builder_keeps_only_portable_text_turns():
    prompt = build_native_prompt([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "content": "secret tool output"},
        {"role": "user", "content": [{"type": "text", "text": "structured"}]},
        {"role": "user", "content": "latest"},
    ], "System rule")
    assert "secret tool output" not in prompt and "structured" not in prompt
    assert prompt.index("System rule") < prompt.index("[User]\nfirst") < prompt.index("[Assistant]\nanswer")
    assert prompt.endswith("Final user message:\nlatest")
    assert build_native_prompt([{"role": "user", "content": "only"}], None) == "only"


def test_scrub_and_limit_classification():
    assert "[redacted]" in scrub("token sk-" + "a" * 40 + " failed") and "aaaa" not in scrub("sk-" + "a" * 40)
    assert classify_limit("HTTP 429 Too Many Requests") == LimitStatus.RATE_LIMITED
    assert classify_limit("You have reached your usage limit") == LimitStatus.QUOTA_EXHAUSTED
    assert classify_limit("file not found") is None


@pytest.mark.asyncio
async def test_claude_streams_text_and_reports_usage(monkeypatch):
    agent = PrintAgent("claude", ".")
    agent.ready = True
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-fable-5-1"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "po"}}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ng"}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "pong",
         "usage": {"input_tokens": 2, "cache_read_input_tokens": 10, "output_tokens": 4}},
    ]
    calls = install_fake(monkeypatch, agent, lines)
    events = await collect(agent)
    assert [e.content for e in events if isinstance(e, TextDelta)] == ["po", "ng"]
    assert any(isinstance(e, ThinkingDelta) for e in events)
    assert any(isinstance(e, NativeActivity) and "Read" in e.description for e in events)
    done = events[-1]
    assert isinstance(done, AgentDone) and (done.usage.input_tokens, done.usage.output_tokens) == (12, 4)
    command, stdin_text = calls[0]
    assert stdin_text == "hello" and "--model" in command and command[command.index("--model") + 1] == "fable"
    assert "--append-system-prompt" in command and "--dangerously-skip-permissions" not in command


@pytest.mark.asyncio
async def test_claude_result_text_is_used_when_nothing_streamed(monkeypatch):
    agent = PrintAgent("claude", ".")
    install_fake(monkeypatch, agent, [{"type": "result", "subtype": "success", "result": "complete answer", "usage": {}}])
    events = await collect(agent)
    assert [type(e).__name__ for e in events] == ["TextDelta", "AgentDone"] and events[0].content == "complete answer"


@pytest.mark.asyncio
async def test_claude_rejected_rate_limit_becomes_limit_hit(monkeypatch):
    agent = PrintAgent("claude", ".")
    install_fake(monkeypatch, agent, [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}])
    events = await collect(agent)
    assert isinstance(events[-1], LimitHit) and events[-1].error_type == LimitStatus.QUOTA_EXHAUSTED


@pytest.mark.asyncio
async def test_claude_error_result_raises_scrubbed_message(monkeypatch):
    agent = PrintAgent("claude", ".")
    install_fake(monkeypatch, agent, [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                                       "result": "boom token " + "z" * 40}])
    with pytest.raises(RuntimeError, match=r"could not complete.*\[redacted\]") as info:
        await collect(agent)
    assert "zzzz" not in str(info.value)


@pytest.mark.asyncio
async def test_process_exit_without_result_is_reported(monkeypatch):
    agent = PrintAgent("claude", ".")
    install_fake(monkeypatch, agent, [], returncode=1, stderr=b"Not logged in")
    with pytest.raises(RuntimeError, match="exit code 1.*Not logged in"):
        await collect(agent)


@pytest.mark.asyncio
async def test_antigravity_stream_and_result(monkeypatch):
    agent = PrintAgent("google", ".")
    lines = [
        {"event": "init", "init": {"cwd": "."}},
        {"event": "step_update", "step_update": {"step_type": "user_input", "state": "DONE"}},
        {"event": "step_update", "step_update": {"step_type": "view_file", "state": "ACTIVE"}},
        {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "ACTIVE", "text_delta": "pong"}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "pong\n", "usage": {"input_tokens": 7, "output_tokens": 3}}},
    ]
    calls = install_fake(monkeypatch, agent, lines)
    events = await collect(agent, model="gemini-3.8-flash-high")
    assert [e.content for e in events if isinstance(e, TextDelta)] == ["pong"]
    assert any(isinstance(e, NativeActivity) and "view_file" in e.description for e in events)
    assert isinstance(events[-1], AgentDone) and events[-1].usage.input_tokens == 7
    command, stdin_text = calls[0]
    assert stdin_text is None and command[:2] == ["--print", "Conversation instructions:\nBe brief.\n\nhello"]
    assert "--dangerously-skip-permissions" not in command


@pytest.mark.asyncio
async def test_antigravity_quota_error_becomes_limit_hit(monkeypatch):
    agent = PrintAgent("google", ".")
    install_fake(monkeypatch, agent, [{"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota"}}])
    events = await collect(agent, model="gemini-3.8-flash-high")
    assert isinstance(events[-1], LimitHit) and events[-1].error_type == LimitStatus.QUOTA_EXHAUSTED


@pytest.mark.asyncio
async def test_discovery_parses_model_lists(monkeypatch):
    google = PrintAgent("google", ".")

    async def run_models(command, timeout):
        return 0, "Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n", ""

    monkeypatch.setattr(google, "_run_captured", run_models)
    await google.refresh_models()
    assert google.ready and google.get_default_model() == "gemini-3.1-pro-high"
    assert {m.id: m.tier for m in google.available_models} == {"gemini-3.1-pro-high": "flagship", "gemini-3.8-flash-low": "fast"}
    assert google.get_model_info("brand-new-model").id == "brand-new-model"

    claude = PrintAgent("claude", ".")

    async def logged_out(command, timeout):
        return 0, json.dumps({"loggedIn": False}), ""

    monkeypatch.setattr(claude, "_run_captured", logged_out)
    await claude.refresh_models()
    assert not claude.ready and "no active sign-in" in claude.discovery_error

    async def logged_in(command, timeout):
        assert command == ["auth", "status"]
        return 0, json.dumps({"loggedIn": True}), ""

    monkeypatch.setattr(claude, "_run_captured", logged_in)
    await claude.refresh_models()
    assert claude.ready and claude.get_default_model() == "fable"


@pytest.mark.asyncio
async def test_registry_prefers_direct_keys_and_installed_native_clis(monkeypatch, tmp_path):
    config = ThwipConfig(project=str(tmp_path))
    config.keys = {"openai": "sk-test"}
    registry = AgentRegistry(config)
    available = {"codex": "/bin/codex", "claude": "/bin/claude", "agy": "/bin/agy"}
    monkeypatch.setattr("thwip.agents.native_agent.shutil.which", lambda name: available.get(name))
    monkeypatch.setattr("thwip.agents.native_print.shutil.which", lambda name: available.get(name))

    async def fake_refresh(self):
        self.ready = True
        self.discovery_error = ""

    monkeypatch.setattr(PrintAgent, "refresh_models", fake_refresh)
    await registry.connect_native_agents(str(tmp_path))
    assert not getattr(registry.get_agent("openai"), "native_tools", False)
    assert isinstance(registry.get_agent("claude"), PrintAgent) and registry.get_agent("claude").ready
    google = registry.get_agent("google")
    assert isinstance(google, PrintAgent) and google.binary == "agy"


def test_limit_windows_are_captured_and_rendered():
    from thwip.agents.native_common import describe_limit_windows, window_label

    agent = PrintAgent("claude", ".")
    agent._claude_event({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.67, "resetsAt": 1790196600}, "seven_day": {"utilization": 0.17}}}}, "rate_limit_event")
    assert [w["label"] for w in agent.limit_windows] == ["5h", "7d"]
    text = describe_limit_windows(agent.limit_windows)
    assert text.startswith("5h: 67% used, resets") and "7d: 17% used" in text
    assert describe_limit_windows([]) == "Not reported yet"
    assert (window_label(300), window_label(10080), window_label(None)) == ("5h", "7d", "window")


@pytest.mark.asyncio
async def test_antigravity_network_reset_gets_a_network_hint(monkeypatch):
    agent = PrintAgent("google", ".")
    install_fake(monkeypatch, agent, [{"event": "result", "result": {"status": "ERROR", "error":
        "API error (attempt 1): request failed: Post \"https://example.googleapis.com\": read tcp 10.0.0.2:5->1.2.3.4:443: read: connection reset by peer"}}])
    with pytest.raises(RuntimeError, match="connection reset by peer.*network or a firewall"):
        await collect(agent, model="gemini-3.8-flash-high")
