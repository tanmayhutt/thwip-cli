"""Compaction on a different provider, proactive offers, and quota warnings."""

from types import SimpleNamespace

import pytest

from thwip.agents.base import AgentDone, LimitHit, LimitStatus, TextDelta
from thwip.compaction import choose_worker, compacted_messages, split_for_compaction, summarize, transcript


class Agent:
    capabilities = set()

    def __init__(self, name, reply="summary text", limit=False):
        self.name = name
        self.display_name = name
        self.company = name
        self.reply = reply
        self.limit = limit
        self.calls = 0

    def is_configured(self):
        return True

    def get_default_model(self):
        return f"{self.name}-model"

    def get_handoff_models(self):
        from thwip.agents.base import ModelInfo
        return [ModelInfo(id=f"{self.name}-model", name=self.name)]

    def get_model_info(self, model_id):
        return next((m for m in self.get_handoff_models() if m.id == model_id), None)

    async def chat(self, **kwargs):
        self.calls += 1
        if self.limit:
            yield LimitHit(error_type=LimitStatus.QUOTA_EXHAUSTED, message="usage limit")
            return
        yield TextDelta(content=self.reply)
        yield AgentDone()


def test_worker_is_never_the_active_provider_when_another_is_ready():
    a, b, c = Agent("a"), Agent("b"), Agent("c")
    assert choose_worker(a, [a, b, c]) is b
    assert choose_worker(a, [a, b, c], chain=["c/m", "b/m"]) is c
    assert choose_worker(a, [a]) is a, "last resort: the active provider when nothing else exists"
    assert choose_worker(a, []) is a, "a connected active provider is still a last resort"
    assert choose_worker(SimpleNamespace(name="z", is_configured=lambda: False), []) is None


def test_split_keeps_recent_turns_starting_at_a_user_message():
    history = [{"role": "user", "content": f"q{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"a{i}"} for i in range(10)]
    older, recent = split_for_compaction(history, 3)
    assert [m["content"] for m in recent] == ["q8", "a9"], "an odd cut moves forward to the next user message"
    assert len(older) == 8
    assert split_for_compaction(history, 0) == (history, [])
    assert split_for_compaction(history[:2], 4) == (history[:2], [])
    assert "[User]\nq0" in transcript(history)


@pytest.mark.asyncio
async def test_summarize_uses_the_worker_and_reports_limits():
    worker = Agent("b", reply="  Context\n- things  ")
    assert await summarize(worker, [{"role": "user", "content": "x"}]) == "Context\n- things"
    with pytest.raises(RuntimeError, match="hit a limit"):
        await summarize(Agent("c", limit=True), [{"role": "user", "content": "x"}])
    with pytest.raises(RuntimeError, match="empty summary"):
        await summarize(Agent("d", reply="   "), [{"role": "user", "content": "x"}])
    compact = compacted_messages("S", [{"role": "user", "content": "recent"}])
    assert compact[0]["content"].endswith("S") and compact[-1]["content"] == "recent" and len(compact) == 3


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from thwip.cli import ThwipCLI
    from thwip.config import ThwipConfig
    from thwip.session import Session
    from thwip.tools import ToolManager

    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "cfg"))
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = ThwipConfig(project=str(tmp_path), auto_save=False)
    cli.tool_manager = ToolManager(str(tmp_path))
    cli.session = Session(project_path=str(tmp_path), current_agent="a", current_model="a-model")
    active, other = Agent("a"), Agent("b", reply='{"context": ["long chat"], "decisions": ["CODEWORD is OLIVE"], "open_tasks": []}')
    cli.current_agent = active
    cli.registry = SimpleNamespace(get_ready_agents=lambda: [active, other], get_agent=lambda n: active)
    for i in range(6):
        cli.session.add_user_message(f"question {i}")
        cli.session.add_assistant_message(f"answer {i}", agent_name="a", model="a-model")
    cli.session.set_native_session("a", "thread-1", "a-model")
    return cli, active, other


@pytest.mark.asyncio
async def test_cmd_compact_uses_other_provider_and_keeps_recent_turns(cli):
    cli, active, other = cli
    assert await cli.cmd_compact() is True
    assert other.calls == 1 and active.calls == 0, "the active provider never writes its own summary"
    assert "- CODEWORD is OLIVE" in cli.session.messages[0].content and "Decisions" in cli.session.messages[0].content
    assert [m.content for m in cli.session.archive][:2] == ["question 0", "answer 0"], "older turns are archived, not lost"
    contents = [m.content for m in cli.session.messages]
    assert contents[0].startswith("Summary of the conversation so far") and "OLIVE" in contents[0]
    assert contents[-4:] == ["question 4", "answer 4", "question 5", "answer 5"]
    assert cli.session.native_sessions == {}, "native sessions restart from the compact history"


@pytest.mark.asyncio
async def test_compaction_offer_fires_once_near_the_window(cli, monkeypatch):
    cli, _active, _other = cli
    cli.config.limits.assumed_context_tokens = 300
    cli.config.limits.compact_at_percent = 50
    asked = []

    async def ask(question):
        asked.append(question)
        return "n"
    monkeypatch.setattr(cli, "_ask_text", ask)
    await cli._maybe_offer_compaction()
    await cli._maybe_offer_compaction()
    assert len(asked) == 1 and "using b" in asked[0] and asked[0].startswith("Compact")
    cli.config.limits.assumed_context_tokens = 10_000_000
    cli._compaction_offered_at = 0
    await cli._maybe_offer_compaction()
    assert len(asked) == 1, "no offer when the conversation is small"


def test_quota_warning_recommends_a_ready_alternative(cli, monkeypatch):
    cli, active, _other = cli
    printed = []
    monkeypatch.setattr("thwip.cli.print_warning", lambda message: printed.append(message))
    active.limit_windows = [{"label": "5h", "used_percent": 91, "resets_at": 1790000000},
                            {"label": "7d", "used_percent": 20, "resets_at": None}]
    cli._quota_warning()
    cli._quota_warning()
    assert len(printed) == 1 and "91%" in printed[0] and "/switch b" in printed[0]
    active.limit_windows[0]["resets_at"] = 1790009999
    cli._quota_warning()
    assert len(printed) == 2, "a new window (new reset time) warns again"


@pytest.mark.asyncio
async def test_cmd_compact_falls_back_to_plain_text_when_worker_cannot_do_json(cli):
    cli, _active, other = cli
    other.reply = "Context\n- plain prose summary"
    assert await cli.cmd_compact() is True
    assert other.calls == 4, "two repairs after the first JSON attempt, then one plain-text request"
    assert "plain prose summary" in cli.session.messages[0].content
