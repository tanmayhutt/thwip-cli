"""Commands that mirror Codex, Claude Code, and Antigravity REPL flows, tested offline."""

import subprocess
from pathlib import Path

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
    cli.cmd_new()
    first = Session.list_saved_sessions()[0]["name"]
    await cli.cmd_resume("1")
    assert cli.session.name == first, "/resume 1 picks the first listed session"
    assert cli.session.project_path.startswith("/"), "loaded project path is absolute"


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


def test_known_projects_come_from_sessions_and_scan_roots(cli, tmp_path, monkeypatch):
    from thwip.config import MemoryConfig

    (tmp_path / "scan" / "alpha").mkdir(parents=True)
    (tmp_path / "scan" / "beta").mkdir()
    (tmp_path / "scan" / "beta" / "context.md").write_text("---\nproject: beta\n---\n")
    (tmp_path / "scan" / ".hidden").mkdir()
    recent = tmp_path / "recent"
    recent.mkdir()
    Session(project_path=str(recent), current_agent="openai", current_model="alpha").save("older")
    cli.config.memory = MemoryConfig(scan=[str(tmp_path / "scan")], onboarded=True)
    known = cli._known_projects()
    assert known[0] == recent.resolve(), "projects from saved sessions come first"
    assert {p.name for p in known} == {"recent", "beta", "alpha"}, "hidden folders are skipped, each project listed once"
    assert all(p != Path.home().resolve() for p in known)
    listed = Session.list_saved_sessions()
    assert listed[0]["project"] == str(recent)


@pytest.mark.asyncio
async def test_project_picker_creates_a_project_with_a_context_file(cli, tmp_path, monkeypatch):
    from thwip.config import MemoryConfig

    parent = tmp_path / "scan"
    parent.mkdir()
    cli.config.memory = MemoryConfig(scan=[str(parent)], onboarded=True)
    monkeypatch.setattr(cli.config, "save", lambda: None)
    answers(monkeypatch, cli, "1", "demo-app", "")   # 1 = New project when nothing is known yet
    await cli._choose_project(at_startup=True)
    target = parent / "demo-app"
    assert target.is_dir() and (target / "context.md").is_file()
    assert Path(cli.session.project_path) == target.resolve() and cli.session.native_sessions == {}


@pytest.mark.asyncio
async def test_project_picker_enter_keeps_the_current_project(cli, tmp_path, monkeypatch):
    from thwip.config import MemoryConfig

    cli.config.memory = MemoryConfig(onboarded=True)
    answers(monkeypatch, cli, "")
    before = cli.session.project_path
    await cli._choose_project()
    assert cli.session.project_path == before


@pytest.mark.asyncio
async def test_cli_wording_aliases_map_to_thwip_commands(cli, monkeypatch):
    """The same job has different names across CLIs; thwip accepts each one."""
    seen = []
    monkeypatch.setattr(cli, "cmd_list_sessions", lambda: seen.append("sessions"))
    monkeypatch.setattr(cli, "cmd_permissions", lambda: seen.append("permissions"))
    monkeypatch.setattr(cli, "cmd_show_agents", lambda: seen.append("agents"))
    async def resume(name=""):
        seen.append(f"resume:{name}")
    async def memory(sub="", rest=""):
        seen.append(f"memory:{sub}")
    monkeypatch.setattr(cli, "cmd_resume", resume)
    monkeypatch.setattr(cli, "cmd_memory", memory)
    for line in ("/sessions", "/approvals", "/clis", "/continue 2", "/init"):
        await cli.handle_command(line)
    assert seen == ["sessions", "permissions", "agents", "resume:2", "memory:init"]


@pytest.mark.asyncio
async def test_switch_offers_compaction_when_the_transfer_is_large(cli, monkeypatch):
    from thwip.config import LimitsConfig

    cli.config.limits = LimitsConfig(compact_at_percent=1, assumed_context_tokens=1000)
    for i in range(6):
        cli.session.add_user_message(f"question {i} " + "word " * 200)
        cli.session.add_assistant_message(f"answer {i} " + "word " * 200, agent_name="openai", model="alpha", company="x")
    asked = []
    async def yes_no(question):
        asked.append(question)
        return False
    monkeypatch.setattr(cli, "_ask_yes_no", yes_no)
    monkeypatch.setattr("thwip.cli.sys.stdin.isatty", lambda: True)
    await cli._offer_compaction_before_switch(FakeAgent(), "alpha")
    assert asked and "Compact older turns first" in asked[0]
    # A short conversation is switched without any question.
    asked.clear()
    cli.session.clear_context()
    cli.session.add_user_message("hi")
    cli.session.add_assistant_message("hello", agent_name="openai", model="alpha", company="x")
    await cli._offer_compaction_before_switch(FakeAgent(), "alpha")
    assert not asked


def test_current_folder_wins_over_the_remembered_project(tmp_path, monkeypatch):
    from thwip.cli import ThwipCLI

    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))
    remembered = tmp_path / "remembered"
    here = tmp_path / "here"
    remembered.mkdir(); here.mkdir()
    cfg = ThwipConfig(project=str(remembered))
    cfg.save()
    monkeypatch.chdir(here)
    assert Path(ThwipCLI().config.project) == here.resolve()
    monkeypatch.chdir(Path.home())
    assert Path(ThwipCLI().config.project) == remembered.resolve(), "from home, the remembered project is the default"
    assert Path(ThwipCLI(project=str(here)).config.project) == here.resolve(), "--project always wins"
