"""Project memory file, vault filing with cross-project links, onboarding, and model-driven updates."""

import json

import pytest

from thwip.agents.base import AgentDone, TextDelta
from thwip.cli import ThwipCLI
from thwip.config import ThwipConfig
from thwip.memory import ProjectMemory, Vault, detect_obsidian_vaults, parse_frontmatter, render_frontmatter
from thwip.session import Session
from thwip.tools import ToolManager


def test_frontmatter_round_trip():
    data, body = parse_frontmatter("---\nproject: Demo\nstack: [Python, Docker]\nupdated: 2026-09-24\n---\n\n# Demo\n")
    assert data == {"project": "Demo", "stack": ["Python", "Docker"], "updated": "2026-09-24"} and body.startswith("# Demo")
    assert render_frontmatter({"a": "x", "b": ["1", "2"], "c": ""}) == "---\na: x\nb: [1, 2]\n---"
    assert parse_frontmatter("no frontmatter") == ({}, "no frontmatter")


def test_init_detects_stack_and_never_overwrites(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "package.json").write_text("{}")
    memory = ProjectMemory(str(tmp_path))
    text = memory.init(area="Developer Tools", tags=["cli"])
    data, body = parse_frontmatter(text)
    assert data["project"] == tmp_path.name and data["stack"] == ["Python", "JavaScript"] and data["area"] == "Developer Tools"
    assert data["type"] == "cli-tool" and data["tags"] == ["cli"] and "## Current Work" in body
    memory.write("---\nproject: Custom\n---\n\n# Custom\n")
    assert memory.init() .startswith("---\nproject: Custom"), "init never replaces an existing file"
    assert "Project memory (context.md)" in memory.injection() and "# Custom" in memory.injection()
    memory.write("---\nproject: Big\n---\n" + "x" * 20000)
    assert memory.injection().endswith("[Project memory truncated; read the full file with /memory]")
    assert memory.touch_updated("---\nupdated: 2000-01-01\nproject: Big\n---\n\nbody").split("\n")[1] != "updated: 2000-01-01"


def test_detect_obsidian_vaults_reads_registry(tmp_path, monkeypatch):
    registry = tmp_path / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    registry.parent.mkdir(parents=True)
    vault = tmp_path / "MyVault"
    vault.mkdir()
    registry.write_text(json.dumps({"vaults": {"a": {"path": str(vault)}, "b": {"path": str(tmp_path / "missing")}}}))
    monkeypatch.setattr("thwip.memory.Path.home", lambda: tmp_path)
    assert detect_obsidian_vaults() == [str(vault)]


def test_vault_sync_files_cards_hubs_and_related_links(tmp_path):
    vault = Vault(str(tmp_path / "brain"))
    vault.create()
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    for folder, extra in ((alpha, "Rust"), (beta, "Go")):
        folder.mkdir()
        ProjectMemory(str(folder)).write(
            f"---\nproject: {folder.name}\npurpose: {folder.name} tool\narea: Developer Tools\nstatus: active\n"
            f"updated: 2026-09-24\nstack: [Python, {extra}]\ntags: [cli]\n---\n\n# {folder.name} Context\n")
    report = vault.sync(ProjectMemory(str(alpha)))
    assert any(path.endswith("Projects/alpha.md") for path in report["written"])
    card = (vault.root / "Projects" / "alpha.md").read_text()
    assert "generated_by: thwip" in card and "None yet" in card
    report = vault.sync(ProjectMemory(str(beta)))
    beta_card = (vault.root / "Projects" / "beta.md").read_text()
    assert "[[Projects/alpha|alpha]]: shares area Developer Tools, stack Python, tag cli" in beta_card
    hub = (vault.root / "Stack" / "Python.md").read_text()
    assert "[[Projects/alpha|alpha]]" in hub and "[[Projects/beta|beta]]" in hub
    assert (vault.root / "Areas" / "Developer Tools.md").is_file() and (vault.root / "Tags" / "cli.md").is_file()
    dashboard = (vault.root / "Projects.md").read_text()
    assert "| [[Projects/alpha|alpha]] | Developer Tools | active | Python, Rust | 2026-09-24 |" in dashboard
    # Re-filing alpha now links back to beta.
    vault.sync(ProjectMemory(str(alpha)))
    assert "[[Projects/beta|beta]]" in (vault.root / "Projects" / "alpha.md").read_text()
    # A hand-written note is never overwritten.
    (vault.root / "Projects" / "gamma.md").write_text("# my own note\n")
    gamma = tmp_path / "gamma"
    gamma.mkdir()
    ProjectMemory(str(gamma)).write("---\nproject: gamma\n---\n\n# gamma\n")
    report = vault.sync(ProjectMemory(str(gamma)))
    assert report["skipped"] == [str(vault.root / "Projects" / "gamma.md")]
    assert (vault.root / "Projects" / "gamma.md").read_text() == "# my own note\n"


class FakeAgent:
    name = "openai"
    display_name = "Fake"
    company = "OpenAI"
    native_tools = False

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def is_configured(self):
        return True

    async def chat(self, **kwargs):
        self.prompts.append(kwargs)
        yield TextDelta(content=self.reply)
        yield AgentDone()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = ThwipConfig(project=str(tmp_path / "proj"), auto_save=False)
    (tmp_path / "proj").mkdir()
    cli.config.memory.vault = str(tmp_path / "brain")
    cli.config.memory.onboarded = True
    cli.tool_manager = ToolManager(str(tmp_path / "proj"))
    cli.session = Session(project_path=str(tmp_path / "proj"), current_agent="openai", current_model="m")
    cli.session.add_user_message("we decided to use sqlite")
    cli.session.add_assistant_message("noted", agent_name="openai", model="m")
    return cli


@pytest.mark.asyncio
async def test_memory_update_writes_after_confirmation_and_files_in_vault(cli, monkeypatch):
    memory = cli._memory()
    memory.init(area="Data")
    proposed = memory.read().replace("- Now:", "- Now: use sqlite for storage").replace("## Decisions\n", "## Decisions\n\n- SQLite chosen for zero-setup storage.\n")
    cli.current_agent = FakeAgent("```markdown\n" + proposed + "\n```")
    answers = iter(["n", "y"])

    async def ask(question):
        return next(answers)
    monkeypatch.setattr(cli, "_ask_text", ask)
    assert await cli._memory_update() is False and "sqlite" not in memory.read()
    assert await cli._memory_update() is True
    text = memory.read()
    assert "- Now: use sqlite for storage" in text and "SQLite chosen" in text
    assert "we decided to use sqlite" in cli.current_agent.prompts[0]["messages"][0]["content"]
    assert (tmp_path_card := cli._vault().root / "Projects" / (memory.name() + ".md")).is_file(), tmp_path_card
    # Instructions sent to agents include the memory file.
    assert "Project memory (context.md)" in cli._system_prompt_with_memory() and "SQLite chosen" in cli._system_prompt_with_memory()


@pytest.mark.asyncio
async def test_memory_update_rejects_invalid_model_output(cli, monkeypatch):
    cli._memory().init()
    before = cli._memory().read()
    cli.current_agent = FakeAgent("Sure! Here is a summary instead of a file.")
    assert await cli._memory_update() is False and cli._memory().read() == before


@pytest.mark.asyncio
async def test_memory_commands_show_init_vault_link(cli, monkeypatch, tmp_path):
    await cli.cmd_memory("show")
    await cli.cmd_memory("init", "Developer Tools")
    assert cli._memory().frontmatter()["area"] == "Developer Tools"
    await cli.cmd_memory("vault", str(tmp_path / "other-brain"))
    assert cli.config.memory.vault == str((tmp_path / "other-brain").resolve())
    assert (tmp_path / "other-brain" / "Projects.md").is_file()
    cli._memory_link()
    assert "context.md" in (tmp_path / "proj" / "AGENTS.md").read_text() and "context.md" in (tmp_path / "proj" / "CLAUDE.md").read_text()
    cli._memory_link()  # idempotent
    assert (tmp_path / "proj" / "AGENTS.md").read_text().count("context.md") == 1


@pytest.mark.asyncio
async def test_onboarding_offers_detected_vault_and_saves_choice(cli, monkeypatch, tmp_path):
    cli.config.memory.onboarded = False
    cli.config.memory.vault = ""
    existing = tmp_path / "ObsidianVault"
    existing.mkdir()
    monkeypatch.setattr("thwip.cli.detect_obsidian_vaults", lambda: [str(existing)])
    monkeypatch.setattr("thwip.cli.sys.stdin.isatty", lambda: True)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    async def choose_first(question):
        return "1"
    monkeypatch.setattr(cli, "_ask_text", choose_first)
    await cli._memory_onboarding()
    assert cli.config.memory.vault == str(existing) and cli.config.memory.onboarded
    # Second start: no question asked again.
    async def boom(question):
        raise AssertionError("should not ask again")
    monkeypatch.setattr(cli, "_ask_text", boom)
    await cli._memory_onboarding()
    # Skip choice leaves memory enabled but no vault.
    cli.config.memory.onboarded = False
    async def skip(question):
        return "3"
    monkeypatch.setattr(cli, "_ask_text", skip)
    await cli._memory_onboarding()
    assert cli.config.memory.vault == "" and cli.config.memory.onboarded
    # Creating the default new vault lands under the (patched) home directory.
    cli.config.memory.onboarded = False
    async def create(question):
        return "2"
    monkeypatch.setattr(cli, "_ask_text", create)
    await cli._memory_onboarding()
    assert cli.config.memory.vault == str(tmp_path / "home" / "thwip-brain") and (tmp_path / "home" / "thwip-brain" / "Projects").is_dir()


@pytest.mark.asyncio
async def test_quit_offer_only_with_real_conversation(cli, monkeypatch):
    asked = []
    async def ask(question):
        asked.append(question)
        return "n"
    monkeypatch.setattr(cli, "_ask_text", ask)
    monkeypatch.setattr("thwip.cli.sys.stdin.isatty", lambda: True)
    cli.current_agent = FakeAgent("x")
    await cli._offer_memory_update("quitting")
    assert len(asked) == 1 and "before quitting" in asked[0]
    cli.session.clear_context()
    await cli._offer_memory_update("quitting")
    assert len(asked) == 1
