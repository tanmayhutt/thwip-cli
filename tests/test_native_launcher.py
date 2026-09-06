"""Native launch boundaries without executing a provider or reading credentials."""

from types import SimpleNamespace

import pytest

from thwip.cli import ThwipCLI


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    events = []
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.session = SimpleNamespace(project_path=str(tmp_path))
    cli.session.save = lambda: events.append("save") or tmp_path / "session.json"
    monkeypatch.setattr("thwip.cli.shutil.which", lambda name: "/installed/codex")
    monkeypatch.setattr("thwip.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("thwip.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("thwip.cli.console.input", lambda prompt: "yes")
    monkeypatch.setattr("thwip.cli.print_info", lambda message: None)
    monkeypatch.setattr("thwip.cli.print_error", lambda message: events.append("error"))
    monkeypatch.setattr("thwip.cli.os.execv", lambda path, args: events.append((path, args)))
    return cli, events


@pytest.mark.asyncio
async def test_native_command_saves_before_replacing_process(launcher):
    cli, events = launcher
    await cli.handle_command("/native codex")
    assert events == ["save", ("/installed/codex", [
        "/installed/codex", "--cd", cli.session.project_path,
        "--sandbox", "read-only", "--ask-for-approval", "on-request",
    ])]


@pytest.mark.parametrize("command", ["/native", "/native claude", "/native codex --dangerously-bypass-approvals-and-sandbox"])
@pytest.mark.asyncio
async def test_native_rejects_unsupported_arguments(launcher, command):
    cli, events = launcher
    await cli.handle_command(command)
    assert events == []


def test_native_decline_does_not_save_or_launch(launcher, monkeypatch):
    cli, events = launcher
    monkeypatch.setattr("thwip.cli.console.input", lambda prompt: "")
    cli.cmd_native("codex")
    assert events == []


@pytest.mark.parametrize("failure", ["missing", "noninteractive", "project", "save", "exec"])
def test_native_failure_stays_in_thwip(launcher, monkeypatch, failure):
    cli, events = launcher

    def fail(*args):
        raise OSError("private detail")

    if failure == "missing":
        monkeypatch.setattr("thwip.cli.shutil.which", lambda name: None)
    elif failure == "noninteractive":
        monkeypatch.setattr("thwip.cli.sys.stdin.isatty", lambda: False)
    elif failure == "project":
        cli.session.project_path += "/missing"
    elif failure == "save":
        cli.session.save = fail
    else:
        monkeypatch.setattr("thwip.cli.os.execv", fail)
    cli.cmd_native("codex")
    assert events == (["save", "error"] if failure == "exec" else ["error"])
