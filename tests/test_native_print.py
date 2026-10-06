"""Print-mode native adapters, exercised with fake CLI processes only."""

import asyncio
import json

import pytest

from thwip.agents import AgentRegistry
from thwip.agents.base import (
    AgentDone,
    LimitHit,
    LimitStatus,
    NativeActivity,
    NativePermission,
    TextDelta,
    ThinkingDelta,
)
from thwip.agents.native_common import build_native_prompt, classify_limit, scrub
from thwip.agents.native_print import PrintAgent
from thwip.config import ThwipConfig


class FakeStdin:
    """Records what thwip writes to the CLI. In kept-open mode each user message releases the next batch of output."""

    def __init__(self, process):
        self.process = process
        self.written = []
        self.closed = False

    def write(self, data):
        payload = json.loads(data.decode())
        self.written.append(payload)
        if self.process.keep_open and (payload.get("event") == "user" or payload.get("type") == "user"):
            self.process.feed_next()

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, lines, returncode=0, stderr=b"", keep_open=False):
        # `lines` is one batch (a list of events) or, for kept-open mode, a list of batches, one per turn.
        if keep_open and lines and isinstance(lines[0], dict):
            lines = [lines]   # a single batch
        self.batches = list(lines) if keep_open else [lines]
        self.keep_open = keep_open
        self.stdout = asyncio.StreamReader()
        if not keep_open:
            self.feed_next()
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.stdin = FakeStdin(self)
        self.returncode = None
        self._code = returncode
        self.killed = False
        self.pid = 4242

    def feed_next(self):
        if not self.batches:
            return
        for line in self.batches.pop(0):
            self.stdout.feed_data((json.dumps(line) if not isinstance(line, (bytes, str)) else line).encode()
                                  if not isinstance(line, bytes) else line)
            self.stdout.feed_data(b"\n")

    def user_messages(self):
        return [m["message"]["content"] for m in self.stdin.written if m.get("event") == "user" or m.get("type") == "user"]

    async def wait(self):
        self.returncode = self._code
        return self._code


def install_fake(monkeypatch, agent, lines, returncode=0, stderr=b""):
    """Fake the CLI process. `calls` collects (command, process) per start."""
    calls = []
    keep_open = agent.name == "google"

    async def start(command, stdin_open=False):
        process = FakeProcess(lines, returncode, stderr, keep_open=keep_open)
        calls.append((command, process))
        return process

    monkeypatch.setattr(agent, "_start_process", start)

    def kill(process):
        process.killed = True
        process.returncode = process._code

    monkeypatch.setattr("thwip.agents.native_print.terminate_process_tree", kill)
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
    command, process = calls[0]
    assert process.user_messages() == ["hello"] and command[command.index("--model") + 1] == "fable"
    assert process.stdin.written[0]["request"]["subtype"] == "initialize", "the handshake that routes questions over the stream"
    assert "--append-system-prompt" in command and command[command.index("--permission-prompt-tool") + 1] == "stdio"
    assert "--allowedTools" not in command and "--dangerously-skip-permissions" not in command, "Claude Code keeps its own tools and settings"


@pytest.mark.asyncio
async def test_claude_permission_questions_are_relayed(monkeypatch):
    agent = PrintAgent("claude", ".")
    question = {"type": "control_request", "request_id": "req-1", "request": {
        "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "rm -f build.log"}}}
    done = {"type": "result", "subtype": "success", "result": "done", "usage": {}}
    for approve in (True, False):
        calls = install_fake(monkeypatch, agent, [question, done])
        events = []
        async for event in agent.chat([{"role": "user", "content": "clean up"}], model="fable"):
            if isinstance(event, NativePermission):
                assert "Claude Code wants to use Bash" in event.description and "rm -f build.log" in event.description
                event.approved = approve
            events.append(event)
        assert any(isinstance(e, NativePermission) for e in events) and isinstance(events[-1], AgentDone)
        reply = next(m for m in calls[0][1].stdin.written if m.get("type") == "control_response")
        assert reply["response"]["request_id"] == "req-1"
        assert reply["response"]["response"]["behavior"] == ("allow" if approve else "deny")


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
    command, process = calls[0]
    assert "--print=" in command and command[command.index("--input-format") + 1] == "stream-json"
    assert process.user_messages() == ["Conversation instructions:\nBe brief.\n\nhello"]
    assert "--dangerously-skip-permissions" not in command and "--mode" not in command
    assert agent._kept is not None and agent._kept.alive(), "Antigravity stays open between turns"
    await agent.close()
    assert process.killed and agent._kept is None


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
    assert google.get_model_info("brand-new-model") is None, "Antigravity reports a live list; unlisted IDs are rejected"

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
    assert claude.get_model_info("claude-opus-5-5").id == "claude-opus-5-5" and claude.get_model_info("bogus") is None


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


@pytest.mark.asyncio
async def test_claude_new_session_id_then_resume_flag(monkeypatch):
    agent = PrintAgent("claude", ".")
    calls = install_fake(monkeypatch, agent, [{"type": "system", "subtype": "init", "session_id": "abc-123"},
                                              {"type": "result", "subtype": "success", "result": "hi", "usage": {}}])
    events = await collect(agent)
    assert events[-1].native_session == {"id": "abc-123"}
    command, process = calls[0]
    assert "--session-id" in command and "--resume" not in command and "--append-system-prompt" in command
    assert process.user_messages() == ["hello"]
    # Resume: only the new message, no system prompt re-sent, --resume flag used.
    calls.clear()
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}, {"role": "user", "content": "next"}]
    events = [e async for e in agent.chat(history, model="fable", system_prompt="Be brief.", resume={"id": "abc-123", "synced": 2})]
    command, process = calls[0]
    assert command[command.index("--resume") + 1] == "abc-123" and "--append-system-prompt" not in command
    assert process.user_messages() == ["next"] and events[-1].native_session == {"id": "abc-123"}


@pytest.mark.asyncio
async def test_antigravity_conversation_is_kept_open_and_falls_back(monkeypatch):
    agent = PrintAgent("google", ".")
    turn1 = [{"event": "init", "conversation_id": "conv-1", "init": {}},
             {"event": "result", "result": {"status": "SUCCESS", "response": "hi", "usage": {}}}]
    turn2 = [{"event": "result", "result": {"status": "SUCCESS", "response": "again", "usage": {}}}]
    calls = install_fake(monkeypatch, agent, [turn1, turn2])
    events = await collect(agent, model="gemini-3.8-flash-high")
    assert events[-1].native_session == {"id": "conv-1"} and "--conversation" not in calls[0][0]

    # The resumed turn reuses the open process: no new start, only the new text is sent.
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}, {"role": "user", "content": "next"}]
    events = [e async for e in agent.chat(history, model="gemini-3.8-flash-high", resume={"id": "conv-1", "synced": 2})]
    assert len(calls) == 1 and calls[0][1].user_messages()[-1] == "next"
    assert [e.content for e in events if isinstance(e, TextDelta)] == ["again"]

    # After a restart (no open process), the conversation is resumed by ID; if that fails, a fresh one gets the transcript.
    await agent.close()
    attempts = []
    async def start(command, stdin_open=False):
        attempts.append(command)
        if "--conversation" in command:
            return FakeProcess([[{"event": "result", "result": {"status": "ERROR", "error": "conversation not found"}}]], keep_open=True)
        return FakeProcess([[{"event": "init", "conversation_id": "conv-2", "init": {}},
                             {"event": "result", "result": {"status": "SUCCESS", "response": "fresh", "usage": {}}}]], keep_open=True)
    monkeypatch.setattr(agent, "_start_process", start)
    events = [e async for e in agent.chat(history, model="gemini-3.8-flash-high", resume={"id": "conv-1", "synced": 2})]
    assert len(attempts) == 2 and attempts[0][attempts[0].index("--conversation") + 1] == "conv-1" and "--conversation" not in attempts[1]
    assert "[User]\nhello" in agent._kept.process.user_messages()[0]
    assert any(isinstance(e, NativeActivity) and "unavailable" in e.description for e in events)
    assert events[-1].native_session == {"id": "conv-2"}
    await agent.close()


@pytest.mark.asyncio
async def test_antigravity_empty_answer_surfaces_the_denied_permission(monkeypatch):
    agent = PrintAgent("google", ".")
    install_fake(monkeypatch, agent, [{"event": "result", "result": {"status": "SUCCESS", "response": "", "usage": {}}}],
                 stderr=b"jetski: no output produced \xe2\x80\x94 a tool required the write_file permission that headless mode cannot prompt for, so it was auto-denied.")
    events = await collect(agent, model="gemini-3.8-flash-high")
    texts = [e.content for e in events if isinstance(e, TextDelta)]
    assert texts and "needed a tool permission" in texts[0] and "skip-permissions" in texts[0]
    assert isinstance(events[-1], AgentDone)
    await agent.close()


@pytest.mark.asyncio
async def test_thwip_adds_no_restrictions_of_its_own(monkeypatch):
    claude = PrintAgent("claude", ".")
    calls = install_fake(monkeypatch, claude, [{"type": "result", "subtype": "success", "result": "ok", "usage": {}}])
    await collect(claude)
    command = calls[0][0]
    assert not any(flag.startswith(("--allowedTools", "--disallowedTools", "--permission-mode")) for flag in command)
    agy = PrintAgent("google", ".")
    calls = install_fake(monkeypatch, agy, [[{"event": "result", "result": {"status": "SUCCESS", "response": "ok", "usage": {}}}]])
    await collect(agy, model="gemini-3.8-flash-high")
    assert "--mode" not in calls[0][0] and "--sandbox" not in calls[0][0]
    await agy.close()
