import json
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from thwip.agents import AgentRegistry
from thwip.agents.base import AgentDone, Capability, LimitHit, LimitStatus, TextDelta
from thwip.cli import ThwipCLI
from thwip.config import ThwipConfig, get_usage_path
from thwip.limits import UsageTracker
from thwip.session import Session
from thwip.tools import ToolManager


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv('THWIP_CONFIG_DIR', str(tmp_path / 'config'))
    cli = ThwipCLI.__new__(ThwipCLI)
    cli.config = ThwipConfig(project=str(tmp_path), auto_save=False)
    cli.registry = AgentRegistry(cli.config)
    for a in cli.registry.list_agents():
        monkeypatch.setattr(a, 'is_installed', lambda: True)
        monkeypatch.setattr(a, 'is_configured', lambda: False)
        if a.name == 'ollama':
            a._cached_models = a.get_handoff_models()
    cli.current_agent = cli.registry.get_agent('claude')
    cli.session = Session(project_path=str(tmp_path), current_agent='claude', current_model=cli.current_agent.get_default_model())
    cli.usage_tracker = UsageTracker()
    cli.tool_manager = ToolManager(str(tmp_path))
    cli.detector = SimpleNamespace(scan_all=list)
    monkeypatch.setattr('builtins.input', lambda *args: '')
    monkeypatch.setattr('getpass.getpass', lambda *args: '')
    console = Console(file=StringIO(), width=100, color_system=None)
    monkeypatch.setattr('thwip.cli.console', console)
    monkeypatch.setattr('thwip.theme.console', console)
    return cli


@pytest.mark.parametrize('command', [
    '/help', '/h', '/about', '/guide', '/g', '/info', '/agents', '/a', '/list',
    '/models', '/m', '/models flagship', '/models balanced', '/models fast', '/models google',
    '/tools', '/t', '/status', '/limits', '/detect', '/history', '/clear', '/reset', '/cost',
    '/project', '/session save audit', '/session load missing', '/session list', '/session clear',
    '/key', '/key google', '/key invalid', '/key openai REJECTED_TEST_KEY',
    '/handoff', '/handoff invalid', '/handoff google invalid', '/switch', '/switch invalid',
    '/switch google invalid', '/unknown', '/q', '/exit', '/quit',
])
@pytest.mark.asyncio
async def test_command_no_exception(cli, command):
    await cli.handle_command(command)


@pytest.mark.parametrize('provider', ['claude','google','openai','deepseek','groq','ollama','openrouter'])
@pytest.mark.asyncio
async def test_all_catalogued_handoffs(cli, provider):
    before = list(cli.session.messages)
    for model in cli.registry.get_agent(provider).get_handoff_models():
        await cli.handle_command(f'/handoff {provider} {model.id}')
    assert cli.session.messages == before


@pytest.mark.asyncio
async def test_project_path_with_spaces(cli, tmp_path):
    project = tmp_path / 'project with spaces'
    project.mkdir()
    await cli.handle_command(f'/project {project}')
    assert cli.session.project_path == str(project)


def test_usage_file_private(cli):
    cli.usage_tracker.record_usage('openai', 'gpt-5.6-terra', 10, 5)
    assert get_usage_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('tool,args', [
    ('read_file', {'file_path': 12}),
    ('list_files', {'sub_dir': ['bad']}),
    ('run_python', {'code': None}),
])
def test_invalid_tool_args_return_error_not_exception(cli, tool, args):
    result = cli.tool_manager.execute_tool(tool, args)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_history_displays_literal_markup(cli):
    cli.session.add_user_message('Show [/not-a-tag] literally')
    await cli.handle_command('/history')


@pytest.mark.parametrize('value', ['wrong', None, -1, True])
def test_corrupted_handoff_counter_rejected_or_sanitized(cli, value):
    path = cli.session.save('damaged')
    data = json.loads(path.read_text())
    data['observed_tool_results'] = value
    path.write_text(json.dumps(data))
    loaded = Session.load('damaged')
    assert loaded is None or (type(loaded.observed_tool_results) is int and loaded.observed_tool_results >= 0)


def test_invalid_config_type_rejected_or_defaulted(cli):
    cli.config._apply_toml({'defaults': {'project': 123, 'confirm_tools': 'false'}})
    assert isinstance(cli.config.project, str)
    assert type(cli.config.confirm_tools) is bool


@pytest.mark.asyncio
async def test_session_roundtrip_rebinds_tools(cli, tmp_path):
    target = tmp_path / 'other'
    target.mkdir()
    saved = Session(project_path=str(target), current_agent='google', current_model='missing')
    saved.save('loadable')
    await cli.handle_command('/session load loadable')
    assert cli.tool_manager.file_editor.project_path == target
    assert cli.current_agent.name == 'google'
    assert cli.session.current_model == cli.current_agent.get_default_model()


def test_all_tools_and_safety(cli, tmp_path):
    tm = cli.tool_manager
    assert 'Successfully' in tm.execute_tool('write_file', {'file_path':'a.txt','content':'one'})
    assert tm.execute_tool('read_file', {'file_path':'a.txt'}) == 'one'
    assert 'Successfully' in tm.execute_tool('edit_file', {'file_path':'a.txt','old_str':'one','new_str':'two'})
    assert 'a.txt' in tm.execute_tool('list_files', {})
    assert '42' in tm.execute_tool('run_python', {'code':'print(6 * 7)'})
    assert 'Exit code: 0' in tm.execute_tool('run_command', {'command':'printf audit-ok'})
    assert isinstance(tm.execute_tool('git_status', {}), str)
    assert isinstance(tm.execute_tool('git_diff', {}), str)
    assert 'outside' in tm.execute_tool('read_file', {'file_path':'../outside'})
    outside = tmp_path.parent / 'audit-outside-file'
    outside.write_text('not accessible')
    (tmp_path / 'link').symlink_to(outside)
    assert 'outside' in tm.execute_tool('read_file', {'file_path':'link'})
    assert 'Error' in tm.execute_tool('unknown', {})
    assert {t['name'] for t in tm.get_anthropic_tools()} == {t['function']['name'] for t in tm.get_openai_tools()}


@pytest.mark.asyncio
async def test_failover_does_not_retry_already_failed_provider(cli, monkeypatch):
    agents = [cli.registry.get_agent('claude'), cli.registry.get_agent('google')]
    attempts = []
    for agent in agents:
        monkeypatch.setattr(agent, 'is_configured', lambda: True)
        monkeypatch.setattr(agent, 'get_capabilities_for_model', lambda model: {Capability.CHAT})

        def make_chat(name):
            async def chat(**kwargs):
                attempts.append(name)
                # Stop safely after four simulated rate limits; no actual network calls.
                if len(attempts) <= 4:
                    yield LimitHit(error_type=LimitStatus.RATE_LIMITED, message='test quota')
                else:
                    yield TextDelta(content='stop test')
                    yield AgentDone()
            return chat

        monkeypatch.setattr(agent, 'chat', make_chat(agent.name))
    monkeypatch.setattr(cli.registry, 'get_ready_agents', lambda: agents)
    cli.config.fallback.chain = ['claude', 'google']
    cli.config.limits.auto_switch = True
    await cli.process_user_message('hello')
    assert len(attempts) <= len(agents), f'Repeated failed providers: {attempts}'
