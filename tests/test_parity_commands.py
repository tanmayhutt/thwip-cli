"""Commands that mirror Codex, Claude Code, and Antigravity REPL flows, tested offline."""

import subprocess

import pytest

from thwip.agents import AgentRegistry
from thwip.agents.base import AgentDone, LimitHit, LimitStatus, ModelInfo, TextDelta
from thwip.cli import ThwipCLI
from thwip.config import ThwipConfig
from thwip.limits import UsageTracker
from thwip.session import Session
from thwip.tools import ToolManager


class FakeAgent:
    name = "openai"
    display_name = "Fake"
    company = "OpenAI"
    native_tools = False
    capabilities = set()

    def __init__(self, reply="- Context: built a CLI\n- Decisions: use pty\n- Open tasks: release"):
        self.available_models = [ModelInfo(id="alpha", name="Alpha", is_default=True), ModelInfo(id="beta", name="Beta", tier="fast")]
        self.reply = reply
        self.requests = []

    def is_configured(self):
        return True

    def is_installed(self):
        return True

    def get_model_info(self, model_id):
        return next((m for m in self.available_models if m.id == model_id), None)

    def get_default_model(self):
        return "alpha"

    def get_capabilities_for_model(self, model):
        return set()

    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        if self.reply == "LIMIT":
            yield LimitHit(error_type=LimitStatus.QUOTA_EXHAUSTED, message="usage limit")
            return
        yield TextDelta(content=self.reply)
        yield AgentDone()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = ThwipConfig(project=str(tmp_path), auto_save=True)
    cli.registry = AgentRegistry(cli.config)
    cli.usage_tracker = UsageTracker()
    cli.tool_manager = ToolManager(str(tmp_path))
    cli.session = Session(project_path=str(tmp_path), current_agent="openai", current_model="alpha")
    cli.current_agent = FakeAgent()
    return cli


def answers(monkeypatch, cli, *values):
    queue = list(values)

    async def ask(question):
        return queue.pop(0)
    monkeypatch.setattr(cli, "_ask_text", ask)


@pytest.mark.asyncio
async def test_model_picker_by_number_id_and_unknown(cli, monkeypatch):
    answers(monkeypatch, cli, "2")
    await cli.cmd_model()
    assert cli.session.current_model == "beta"
    await cli.cmd_model("alpha")
    assert cli.session.current_model == "alpha"
    await cli.cmd_model("nope")
    assert cli.session.current_model == "alpha"
    answers(monkeypatch, cli, "")
    await cli.cmd_model()
    assert cli.session.current_model == "alpha"


@pytest.mark.asyncio
async def test_new_saves_previous_and_resume_restores_it(cli, monkeypatch):
    cli.session.add_user_message("hello")
    cli.session.add_assistant_message("hi", agent_name="openai", model="alpha")
    old_name = cli.session.name
    cli.cmd_new()
    assert cli.session.messages == [] and cli.session.name != old_name
    assert Session.load(old_name) is not None
    answers(monkeypatch, cli, "1")
    await cli.cmd_resume()
    assert cli.session.name == old_name and len(cli.session.messages) == 2
    cli.cmd_new()
    await cli.cmd_resume(old_name)
    assert cli.session.name == old_name


@pytest.mark.asyncio
async def test_compact_replaces_history_with_portable_summary(cli):
    cli.session.add_user_message("build a cli")
    cli.session.add_assistant_message("done with pty", agent_name="openai", model="alpha")
    cli.session.add_user_message("now release")
    cli.session.add_assistant_message("ok", agent_name="openai", model="alpha")
    await cli.cmd_compact()
    assert len(cli.session.messages) == 2
    assert "Decisions: use pty" in cli.session.messages[0].content and cli.session.messages[0].role == "user"
    assert cli.session.messages[1].role == "assistant" and cli.session.messages[1].model == "alpha"
    sent = cli.current_agent.requests[0]
    assert "now release" in sent["messages"][0]["content"] and sent["tools"] is None


@pytest.mark.asyncio
async def test_compact_keeps_history_on_limit_or_empty(cli):
    cli.session.add_user_message("a")
    cli.session.add_assistant_message("b", agent_name="openai", model="alpha")
    cli.current_agent = FakeAgent(reply="LIMIT")
    await cli.cmd_compact()
    assert len(cli.session.messages) == 2
    cli.current_agent = FakeAgent(reply="   ")
    await cli.cmd_compact()
    assert len(cli.session.messages) == 2


def test_diff_uses_project_repo(cli, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    cli.cmd_diff("")          # clean working tree message
    (tmp_path / "a.txt").write_text("two\n")
    cli.cmd_diff("")          # renders a diff
    cli.cmd_diff("staged")    # staged view


def test_copy_uses_available_clipboard_tool(cli, monkeypatch):
    calls = []
    monkeypatch.setattr("thwip.cli.shutil.which", lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs["input"])))
    cli.cmd_copy()
    assert calls == []
    cli.session.add_assistant_message("copy me", agent_name="openai", model="alpha")
    cli.cmd_copy()
    assert calls == [(["pbcopy"], b"copy me")]


def test_export_writes_markdown_with_attribution(cli, tmp_path):
    cli.session.add_user_message("question")
    cli.session.add_assistant_message("answer", agent_name="openai", model="alpha", company="OpenAI")
    cli.cmd_export("notes/out.md")
    text = (tmp_path / "notes" / "out.md").read_text()
    assert "## You\n\nquestion" in text and "## Assistant (OpenAI / alpha)\n\nanswer" in text
    cli.cmd_export("")
    assert (tmp_path / f"{cli.session.name}.md").exists()


@pytest.mark.asyncio
async def test_shell_escape_runs_in_project(cli, tmp_path, capsys):
    (tmp_path / "marker.txt").write_text("x")
    await cli.cmd_shell("ls")
    assert cli.session.messages == []
    await cli.cmd_shell("")


def test_mentions_attach_project_files_only(cli, tmp_path):
    (tmp_path / "notes.md").write_text("secret plan")
    expanded = cli._expand_mentions("Review @notes.md and @missing.txt and @../etc/passwd please")
    assert "secret plan" in expanded and "[Attached file: notes.md]" in expanded
    assert "missing.txt]" not in expanded and "passwd]" not in expanded
    assert cli._expand_mentions("email me at user@example.com") == "email me at user@example.com"


def test_incremental_prompt_builder():
    from thwip.agents.native_common import build_incremental_prompt

    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    full, is_full = build_incremental_prompt(history, "sys", 0)
    assert is_full and "[User]\na" in full and full.endswith("c")
    only_new, is_full = build_incremental_prompt(history, "sys", 2)
    assert (only_new, is_full) == ("c", False)
    _bad, is_full = build_incremental_prompt(history, None, 7)
    assert is_full, "a synced count beyond the history falls back to the full transcript"


def test_man_page_commands(tmp_path, monkeypatch, capsys):
    from thwip import cli as cli_module

    monkeypatch.setenv("THWIP_MAN_DIR", str(tmp_path / "man1"))
    assert cli_module.MAN_PAGE.is_file() and cli_module.MAN_PAGE.read_text().startswith(".TH THWIP 1")
    assert cli_module.run_man_command("install-man") == 0
    assert (tmp_path / "man1" / "thwip.1").read_text() == cli_module.MAN_PAGE.read_text()
    monkeypatch.setattr("thwip.cli.shutil.which", lambda name: None)
    assert cli_module.run_man_command("man") == 0
    assert "universal coding agent multiplexer" in capsys.readouterr().out
    with pytest.raises(SystemExit) as info:
        cli_module.main(["install-man"])
    assert info.value.code == 0
