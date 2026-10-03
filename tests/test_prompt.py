"""What assistants are told: no imposed persona, user instructions first, tool claims only when tools are offered."""

from types import SimpleNamespace

import pytest

from thwip.cli import THWIP_NOTE_NATIVE, THWIP_NOTE_TOOLS, ThwipCLI, build_instructions
from thwip.config import ThwipConfig
from thwip.session import Session


def test_default_instructions_have_no_persona():
    text = build_instructions("", "", native=True, tools_offered=False)
    assert "software engineer" not in text.lower() and "expert" not in text.lower()
    assert text == THWIP_NOTE_NATIVE and "Keep your own tools" in text
    direct = build_instructions("", "", native=False, tools_offered=False)
    assert "tools" not in direct.lower(), "no tool claim when thwip offers no tools"
    with_tools = build_instructions("", "", native=False, tools_offered=True)
    assert THWIP_NOTE_TOOLS in with_tools
    assert THWIP_NOTE_TOOLS not in build_instructions("", "", native=True, tools_offered=True), "native CLIs keep their own tools"


def test_user_instructions_come_first_and_memory_last():
    text = build_instructions("Answer in Hindi. Be brief.", "Project memory (context.md)...", native=False, tools_offered=False)
    assert text.startswith("Answer in Hindi. Be brief.") and text.endswith("Project memory (context.md)...")


def test_session_default_is_empty_and_config_fills_it(tmp_path, monkeypatch):
    assert Session().system_prompt == ""
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    import tomllib
    config = ThwipConfig()
    config._apply_toml(tomllib.loads('[defaults]\nsystem_prompt = "You help a biology student."\n'))
    assert config.system_prompt == "You help a biology student."
    config.save()
    assert ThwipConfig.load().system_prompt == "You help a biology student."


@pytest.mark.asyncio
async def test_prompt_command_sets_resets_and_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = ThwipConfig(project=str(tmp_path))
    cli.session = Session(project_path=str(tmp_path))
    cli.current_agent = SimpleNamespace(native_tools=True)
    await cli.cmd_prompt("set", "Explain everything like I am new to programming.")
    assert cli.session.system_prompt.startswith("Explain everything")
    await cli.cmd_prompt("save")
    assert ThwipConfig.load().system_prompt.startswith("Explain everything")
    await cli.cmd_prompt("show")
    await cli.cmd_prompt("reset")
    assert cli.session.system_prompt == ""
    cli.config.memory.enabled = False
    assert cli._system_prompt_with_memory("hello", native=True) == THWIP_NOTE_NATIVE
