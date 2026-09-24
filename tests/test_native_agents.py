"""Native protocol behavior without credentials or model requests."""

import asyncio
from types import SimpleNamespace

import pytest

from thwip.agents.base import AgentDone, LimitHit, LimitStatus, ModelInfo, NativePermission, TextDelta
from thwip.agents.native_agent import NativeAgent


class FakeRPC:
    def __init__(self, provider, failure=False):
        self.events = asyncio.Queue()
        self.requests = []
        self.sent = []
        self.closed = False
        self.provider = provider
        self.failure = failure
        self.process = SimpleNamespace(returncode=None)  # looks alive, like a running app-server
        self.resume_fails = False

    async def request(self, method, params, **kwargs):
        self.requests.append((method, params))
        if method == 'account/read':
            return {'account': {'type': 'chatgpt'}}
        if method == 'model/list':
            return {'data': [{'model': 'future-model', 'displayName': 'Future', 'isDefault': True}]}
        if method == 'session/new':
            return {'sessionId': 's', 'models': {'currentModelId': 'future-model',
                    'availableModels': [{'modelId': 'future-model', 'name': 'Future'}]}}
        if method == 'thread/start':
            return {'thread': {'id': 't'}}
        if method == 'thread/resume':
            if self.resume_fails:
                raise RuntimeError('Native CLI rejected thread/resume (code -32600): unknown thread')
            return {'thread': {'id': params['threadId']}}
        if method == 'turn/start':
            await self.events.put({'id': 99, 'method': 'item/commandExecution/requestApproval',
                                   'params': {'command': 'touch example'}})
            return {}
        if method == 'session/prompt':
            await self.events.put({'id': 99, 'method': 'session/request_permission', 'params': {
                'toolCall': {'title': 'Edit'}, 'options': [
                    {'kind': 'allow_once', 'optionId': 'yes'}, {'kind': 'reject_once', 'optionId': 'no'}]}})
            while not self.sent:
                await asyncio.sleep(0)
            await self.events.put({'method': 'session/update', 'params': {'update': {
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'done'}}}})
            return {'stopReason': 'end_turn'}
        return {}

    async def send(self, message):
        self.sent.append(message)
        if self.provider == 'openai':
            await self.events.put({'method': 'item/agentMessage/delta', 'params': {'itemId': 'a', 'delta': 'done'}})
            await self.events.put({'method': 'turn/completed', 'params': {
                'turn': {'status': 'failed' if self.failure else 'completed'}}})

    async def close(self):
        self.closed = True
        self.process = SimpleNamespace(returncode=0)


@pytest.mark.asyncio
async def test_discovered_models_replace_bundled_catalog(monkeypatch):
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    assert agent.get_model_info('explicit-new-model').id == 'explicit-new-model', 'pass-through before discovery'
    await agent.refresh_models()
    assert agent.ready and agent.get_default_model() == 'future-model'
    assert agent.get_model_info('explicit-new-model') is None, 'only listed IDs once the CLI reported its list'
    assert rpc.closed


def test_native_agent_is_codex_only():
    with pytest.raises(ValueError):
        NativeAgent('google', '.')


@pytest.mark.parametrize('provider', ['openai'])
@pytest.mark.parametrize('approve', [False, True])
@pytest.mark.asyncio
async def test_native_permission_response_and_completion(provider, approve, monkeypatch):
    agent = NativeAgent(provider, '.')
    rpc = FakeRPC(provider)
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    events = []
    async for event in agent.chat([{'role': 'user', 'content': 'hello'}], model='future-model'):
        events.append(event)
        if isinstance(event, NativePermission):
            event.approved = approve
    assert any(isinstance(event, TextDelta) and event.content == 'done' for event in events)
    assert isinstance(events[-1], AgentDone) and events[-1].native_session == {'id': 't'}
    response = rpc.sent[0]['result']
    assert response['decision'] == ('accept' if approve else 'decline')
    assert not rpc.closed, 'the app-server stays alive between turns'
    await agent.close()
    assert rpc.closed


@pytest.mark.asyncio
async def test_failed_turn_never_emits_success(monkeypatch):
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai', failure=True)
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    events = []
    with pytest.raises(RuntimeError, match='could not complete'):
        async for event in agent.chat([], model='future-model'):
            events.append(event)
    assert not any(isinstance(event, AgentDone) for event in events)
    assert rpc.closed


@pytest.mark.asyncio
async def test_discovery_failure_marks_connection_unready(monkeypatch):
    agent = NativeAgent('openai', '.')
    async def connect():
        raise TimeoutError()
    monkeypatch.setattr(agent, '_connect', connect)
    await agent.refresh_models()
    assert not agent.ready and 'timed out' in agent.discovery_error


@pytest.mark.asyncio
async def test_codex_thread_uses_protocol_sandbox_spelling(monkeypatch):
    """Codex App Server rejects camelCase sandbox modes with an invalid-request error."""
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    async for _ in agent.chat([{'role': 'user', 'content': 'hello'}], model='future-model', system_prompt='Be terse.'):
        pass
    _method, params = next(request for request in rpc.requests if request[0] == 'thread/start')
    assert params['sandbox'] == 'read-only' and params['approvalPolicy'] == 'on-request'
    assert params['developerInstructions'] == 'Be terse.'
    turn = next(params for method, params in rpc.requests if method == 'turn/start')
    assert turn['input'][0]['text'] == 'hello'


@pytest.mark.asyncio
async def test_codex_usage_limit_failure_becomes_limit_hit(monkeypatch):
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    async def send(message):
        rpc.sent.append(message)
        await rpc.events.put({'method': 'turn/completed', 'params': {'turn': {
            'status': 'failed', 'error': {'message': 'You have hit your usage limit.'}}}})
    rpc.send = send
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    events = [event async for event in agent.chat([{'role': 'user', 'content': 'hi'}], model='future-model')]
    assert isinstance(events[-1], LimitHit) and events[-1].error_type == LimitStatus.QUOTA_EXHAUSTED
    await agent.close()
    assert rpc.closed


def test_permission_descriptions_are_readable_and_scrubbed():
    from thwip.agents.native_agent import describe_permission

    text = describe_permission({"type": "commandExecution", "command": "curl -H 'Authorization: Bearer " + "k" * 40 + "'",
                                "cwd": "/repo", "reason": "Fetch data"}, "Codex")
    assert text.startswith("Codex wants to run a command.") and "Directory: /repo" in text and "Reason:    Fetch data" in text
    assert "kkkk" not in text and "[redacted]" in text
    files = describe_permission({"type": "fileChange", "changes": [{"path": "a.py", "kind": "update"}]}, "Codex")
    assert "wants to change files" in files and "a.py (update)" in files
    assert "Gemini requests permission for Edit" in describe_permission({"title": "Edit"}, "Gemini")


def test_handoff_accepts_explicit_native_model_ids():
    from thwip.handoff import build_handoff_report, local_model
    from thwip.session import Session

    agent = NativeAgent('openai', '.')
    agent.available_models = [ModelInfo(id='listed', name='Listed', is_default=True)]
    assert local_model(agent, 'listed').name == 'Listed'
    assert local_model(agent, 'brand-new').id == 'brand-new'
    assert local_model(agent, 'has space') is None
    session = Session(current_agent='openai', current_model='listed')
    report = build_handoff_report(session, agent, agent, 'brand-new')
    assert report.target == 'openai/brand-new' and report.context_pressure == 'unknown'


@pytest.mark.asyncio
async def test_codex_rate_limit_notification_is_recorded(monkeypatch):
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    async def send(message):
        rpc.sent.append(message)
        await rpc.events.put({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {
            'primary': {'usedPercent': 2, 'windowDurationMins': 300, 'resetsAt': 1790212672},
            'secondary': {'usedPercent': 23, 'windowDurationMins': 10080}}}})
        await rpc.events.put({'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}})
    rpc.send = send
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    events = [event async for event in agent.chat([{'role': 'user', 'content': 'hi'}], model='future-model')]
    assert isinstance(events[-1], AgentDone)
    assert agent.limit_windows == [
        {'label': '5h', 'used_percent': 2, 'resets_at': 1790212672},
        {'label': '7d', 'used_percent': 23, 'resets_at': None}]


def _turn_prompt(rpc):
    return next(params for method, params in rpc.requests if method == 'turn/start')['input'][0]['text']


@pytest.mark.asyncio
async def test_codex_resumes_thread_and_sends_only_new_messages(monkeypatch):
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    history = [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'one'},
               {'role': 'user', 'content': 'second'}]
    events = [e async for e in agent.chat(history, model='future-model', resume={'id': 'thread-9', 'synced': 2})]
    assert isinstance(events[-1], AgentDone) and events[-1].native_session == {'id': 'thread-9'}
    methods = [m for m, _ in rpc.requests]
    assert 'thread/resume' in methods and 'thread/start' not in methods
    assert _turn_prompt(rpc) == 'second', 'only the new message is sent to a resumed thread'
    # Same process, same thread: no second resume request.
    rpc.requests.clear()
    history += [{'role': 'assistant', 'content': 'two'}, {'role': 'user', 'content': 'third'}]
    [e async for e in agent.chat(history, model='future-model', resume={'id': 'thread-9', 'synced': 4})]
    assert 'thread/resume' not in [m for m, _ in rpc.requests] and _turn_prompt(rpc) == 'third'


@pytest.mark.asyncio
async def test_codex_catch_up_block_after_other_providers_answered(monkeypatch):
    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    history = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'},
               {'role': 'user', 'content': 'c'}, {'role': 'assistant', 'content': 'd by claude'},
               {'role': 'user', 'content': 'e'}]
    [e async for e in agent.chat(history, model='future-model', resume={'id': 'thread-9', 'synced': 2})]
    prompt = _turn_prompt(rpc)
    assert 'Missed conversation' in prompt and 'd by claude' in prompt and prompt.endswith('Final user message:\ne')
    assert '[User]\na' not in prompt, 'messages the thread already saw are not resent'


@pytest.mark.asyncio
async def test_codex_falls_back_to_new_thread_when_resume_fails(monkeypatch):
    from thwip.agents.base import NativeActivity

    agent = NativeAgent('openai', '.')
    rpc = FakeRPC('openai')
    rpc.resume_fails = True
    async def connect():
        return rpc
    monkeypatch.setattr(agent, '_connect', connect)
    history = [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'one'},
               {'role': 'user', 'content': 'second'}]
    events = [e async for e in agent.chat(history, model='future-model', resume={'id': 'gone', 'synced': 2})]
    assert any(isinstance(e, NativeActivity) and 'unavailable' in e.description for e in events)
    start = next(params for method, params in rpc.requests if method == 'thread/start')
    assert start['ephemeral'] is False
    assert '[User]\nfirst' in _turn_prompt(rpc), 'full transcript goes to the new thread'
    assert events[-1].native_session == {'id': 't'}
