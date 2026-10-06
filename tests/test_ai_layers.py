"""Tracing, structured outputs with repair, output guardrails, recall over archived turns."""

import json
import os

import pytest

from thwip import tracing
from thwip.agents.base import AgentDone, TextDelta
from thwip.guardrails import check_output, mask_secrets
from thwip.recall import archive_chunks, excerpt, render
from thwip.session import Message, Session
from thwip.structured import StructuredOutputError, extract_json, render_summary, request_json, summary_schema, validate


def test_tracing_records_and_summarizes(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    tracing.record(tracing.Trace(provider="openai", model="m", latency_s=2.0, input_tokens=10, output_tokens=5, cost_usd=0.01))
    tracing.record(tracing.Trace(provider="openai", model="m", latency_s=4.0, input_tokens=20, output_tokens=5, error="limit"))
    tracing.record(tracing.Trace(provider="claude", model="f", kind="compaction", latency_s=1.0))
    rows = tracing.tail(10)
    assert len(rows) == 3 and rows[-1]["kind"] == "compaction"
    assert tracing.tail(10, provider="openai")[0]["input_tokens"] == 10
    summary = tracing.summarize(rows)
    assert summary["openai"] == {"requests": 2, "input_tokens": 30, "output_tokens": 10, "cost_usd": 0.01, "errors": 1,
                                 "tool_calls": 0, "mean_latency_s": 3.0}
    assert os.name != "posix" or oct((tmp_path / "traces.jsonl").stat().st_mode)[-3:] == "600"
    (tmp_path / "traces.jsonl").write_text("not json\n" + (tmp_path / "traces.jsonl").read_text())
    assert len(tracing.tail(10)) == 3, "a corrupt line is skipped"


def test_structured_validation_and_extraction():
    schema = summary_schema()
    assert validate({"context": ["a"], "decisions": [], "open_tasks": []}, schema) is None
    assert validate({"context": [], "decisions": [], "open_tasks": []}, schema).startswith("context: expected at least 1")
    assert validate({"context": ["a"]}, schema) == "missing required key 'decisions'"
    assert validate({"context": [1], "decisions": [], "open_tasks": []}, schema) == "context: item 0: expected string, got int"
    assert validate(True, {"type": "integer"}) == "expected integer, got bool"
    assert extract_json('Sure! ```json\n{"a": 1}\n``` done') == {"a": 1}
    assert extract_json('prefix {"a": [1, 2]} suffix') == {"a": [1, 2]}
    assert render_summary({"context": ["x"], "decisions": [], "open_tasks": [" y "]}) == "Context\n- x\nDecisions\n- None.\nOpen tasks\n- y"


class ScriptedAgent:
    display_name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def get_default_model(self):
        return "m"

    async def chat(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        yield TextDelta(content=self.replies.pop(0))
        yield AgentDone()


@pytest.mark.asyncio
async def test_request_json_repairs_then_succeeds_or_gives_up():
    agent = ScriptedAgent(["here you go: {\"context\": [\"a\"]}", '{"context": ["a"], "decisions": [], "open_tasks": []}'])
    data, repairs = await request_json(agent, "Summarize", summary_schema())
    assert repairs == 1 and data["context"] == ["a"]
    assert "rejected: missing required key 'decisions'" in agent.prompts[1]
    hopeless = ScriptedAgent(["nope", "still nope", "no"])
    with pytest.raises(StructuredOutputError, match="invalid after 2 repair attempts"):
        await request_json(hopeless, "Summarize", summary_schema())


def test_guardrails_mask_secrets_and_flag_empty():
    result = check_output("The key is sk-" + "a" * 32 + " and AIza" + "B" * 33 + " ok")
    assert result.flagged and result.findings == ["OpenAI-style key", "Google API key"]
    assert "sk-aaaa" not in result.text and "[OpenAI-style key redacted]" in result.text
    assert check_output("   ").findings == ["empty answer"]
    assert not mask_secrets("normal text about keys and tokens").flagged


def test_recall_ranks_archived_turns_and_renders():
    archive = [Message(role="user", content="Remember CODEWORD-x is TEAL."), Message(role="assistant", content="OK"),
               Message(role="user", content="How is the weather?"), Message(role="assistant", content="Sunny.")]
    assert len(archive_chunks(archive)) == 4
    found = excerpt(archive, "what is CODEWORD-x")
    assert found and "TEAL" in found[0].text and found[0].heading == "User"
    assert render(found).startswith("[Earlier conversation excerpts") and render([]) == ""
    assert excerpt([], "anything") == [] and excerpt(archive, "   ") == []


def test_session_archive_persists_and_clear_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    session = Session(project_path=str(tmp_path))
    session.add_user_message("old question")
    session.add_assistant_message("old answer", agent_name="a", model="m")
    session.archive_messages(list(session.messages))
    session.clear_context(keep_archive=True)
    assert session.messages == [] and len(session.archive) == 2
    session.save("archived")
    loaded = Session.load("archived")
    assert [m.content for m in loaded.archive] == ["old question", "old answer"]
    loaded.clear_context()
    assert loaded.archive == []
    path = tmp_path / "sessions" / "archived.json"
    raw = json.loads(path.read_text()); raw["archive"] = [{"role": "tool", "content": "x"}]
    path.write_text(json.dumps(raw))
    assert Session.load("archived") is None, "a malformed archive rejects the file"
