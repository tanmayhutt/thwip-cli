"""Native Codex integration over the Codex App Server JSON-RPC protocol."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path

from thwip import __version__
from thwip.agents.base import (
    AgentDone,
    BaseAgent,
    Capability,
    LimitHit,
    LimitStatus,
    ModelInfo,
    NativeActivity,
    NativePermission,
    SubscriptionInfo,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
)
from thwip.agents.native_common import build_native_prompt, classify_limit, scrub, window_label
from thwip.agents.native_rpc import NativeRPC


def describe_permission(details: dict, provider: str = "Codex") -> str:
    """Summarize a native approval request for the terminal without raw protocol payloads."""
    if not isinstance(details, dict):
        return f"{provider} requests permission for an operation."
    lines = []
    kind = details.get("type") or details.get("kind") or ""
    if kind == "commandExecution" or details.get("command"):
        lines.append(f"{provider} wants to run a command.")
        lines.append(f"Command:   {scrub(details.get('command', ''), 400)}")
        if details.get("cwd"):
            lines.append(f"Directory: {scrub(details['cwd'], 200)}")
    elif kind == "fileChange" or details.get("changes"):
        lines.append(f"{provider} wants to change files.")
        for change in (details.get("changes") or [])[:10]:
            if isinstance(change, dict):
                lines.append(f"File:      {scrub(change.get('path', ''), 200)} ({scrub(change.get('kind', 'edit'), 40)})")
    else:
        title = details.get("title") or details.get("name") or kind or "an operation"
        lines.append(f"{provider} requests permission for {scrub(title, 200)}.")
    if details.get("reason"):
        lines.append(f"Reason:    {scrub(details['reason'], 300)}")
    lines.append("Denying keeps the workspace unchanged; the CLI continues without the operation.")
    return "\n".join(lines)


class NativeAgent(BaseAgent):
    native_tools = True
    capabilities = {Capability.CHAT, Capability.FILE_READ, Capability.FILE_EDIT,
                    Capability.CODE_RUN, Capability.TERMINAL, Capability.GIT}

    def __init__(self, provider: str, project: str):
        if provider != "openai":
            raise ValueError("NativeAgent supports the Codex CLI only; Google uses the Antigravity CLI print adapter.")
        self.name = provider
        self.binary = "codex"
        self.company = "OpenAI"
        self.display_name = "Codex CLI"
        self.project = str(Path(project).expanduser().resolve())
        self.available_models = []
        self.ready = False
        self.discovery_error = "Not connected yet"
        # Usage windows the CLI reports for its own account; informational only.
        self.limit_windows: list[dict] = []

    def is_installed(self):
        return shutil.which(self.binary) is not None

    def is_configured(self):
        return self.ready

    @property
    def auth_method(self):
        return "native_cli"

    def get_status_display(self):
        return (("Ready (existing CLI login)", "status.ready") if self.ready else
                ("CLI connection unavailable; /models retries", "status.limited"))

    def get_install_info(self):
        return {"method": "Native CLI protocol", "path": shutil.which(self.binary) or "", "version": ""}

    def get_subscription_info(self):
        return SubscriptionInfo(is_active=self.ready, message="Authentication and billing managed by installed CLI")

    def check_limits(self):
        return LimitStatus.OK if self.ready else LimitStatus.UNKNOWN

    def get_model_info(self, model_id):
        known = super().get_model_info(model_id)
        if known or self.ready:
            # Once the CLI has reported its list, only listed IDs are accepted.
            return known
        if not model_id or not isinstance(model_id, str) or any(c.isspace() for c in model_id):
            return None
        return ModelInfo(id=model_id, name=model_id)

    async def _connect(self):
        executable = shutil.which(self.binary)
        if not executable:
            raise RuntimeError(f"{self.display_name} is no longer installed.")
        command = [executable, "app-server"]
        rpc = await NativeRPC(command, self.project).start()
        try:
            await rpc.request("initialize", {"clientInfo": {"name": "thwip", "version": __version__}})
            await rpc.send({"method": "initialized", "params": {}})
            return rpc
        except BaseException:
            await rpc.close()
            raise

    async def refresh_models(self):
        rpc = None
        try:
            rpc = await self._connect()
            models = []
            auth = await rpc.request("account/read", {"refreshToken": False})
            if not auth.get("account") and auth.get("requiresOpenaiAuth", True):
                raise RuntimeError("Codex has no active sign-in. Open codex to restore its login.")
            cursor = None
            for _ in range(20):
                page = await rpc.request("model/list", {"limit": 100, "cursor": cursor})
                for item in page.get("data", []):
                    models.append(ModelInfo(id=item["model"], name=item.get("displayName", item["model"]),
                                            is_default=item.get("isDefault", False),
                                            supports_thinking=bool(item.get("supportedReasoningEfforts"))))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if not models:
                raise RuntimeError("The installed CLI returned no selectable models.")
            self.available_models = list({model.id: model for model in models}.values())
            self.ready = True
            self.discovery_error = ""
        except (OSError, RuntimeError, TimeoutError, KeyError, TypeError) as exc:
            self.ready = False
            self.discovery_error = str(exc) or "Native CLI discovery timed out."
        finally:
            if rpc:
                await rpc.close()

    async def chat(self, messages, model=None, system_prompt=None, tools=None, stream=True):
        rpc = await self._connect()
        event_task = None
        try:
            # A fresh native session receives only portable text; tool state stays native within the turn.
            prompt = build_native_prompt(messages, None)
            # Codex App Server sandbox modes are kebab-case; camelCase is rejected as an invalid request.
            session = await rpc.request("thread/start", {
                "cwd": self.project, "model": model or self.get_default_model(), "sandbox": "read-only",
                "approvalPolicy": "on-request", "ephemeral": True,
                **({"developerInstructions": system_prompt} if system_prompt else {}),
            })
            thread_id = session["thread"]["id"]
            await rpc.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})
            usage = TokenUsage()
            started_items = {}
            text_items = set()
            while True:
                event_task = asyncio.create_task(rpc.events.get())
                done, _ = await asyncio.wait([event_task], timeout=600)
                if not done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    raise TimeoutError("Native agent response timed out.")
                event = event_task.result()
                method, params = event.get("method", ""), event.get("params", {})
                if method == "_closed":
                    raise RuntimeError("Native CLI exited before completing the response.")
                if "id" in event:
                    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                        details = {**started_items.get(params.get("itemId"), {}), **params}
                        permission = NativePermission(describe_permission(details, "Codex"))
                        yield permission
                        await rpc.send({"id": event["id"], "result": {"decision": "accept" if permission.approved else "decline"}})
                    else:
                        await rpc.send({"id": event["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
                    continue
                if method == "item/agentMessage/delta":
                    text_items.add(params.get("itemId"))
                    yield TextDelta(content=params.get("delta", ""))
                elif method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
                    yield ThinkingDelta(content=params.get("delta", ""))
                elif method == "error":
                    message = scrub(params.get("error", params).get("message", "") if isinstance(params.get("error", params), dict) else params)
                    limit = classify_limit(message)
                    if limit:
                        yield LimitHit(error_type=limit, message=message)
                        return
                    raise RuntimeError(f"Codex reported an error: {message or 'no details'}")
                elif method == "item/started":
                    item = params.get("item", {})
                    started_items[item.get("id")] = item
                    if item.get("type") not in {"userMessage", "agentMessage", "reasoning", "plan"}:
                        yield NativeActivity(description=f"{item.get('type', 'tool')}: {item.get('command', '')}")
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("id") not in text_items:
                        yield TextDelta(content=item.get("text", ""))
                elif method == "account/rateLimits/updated":
                    limits = params.get("rateLimits", {}) if isinstance(params.get("rateLimits"), dict) else {}
                    windows = []
                    for key in ("primary", "secondary"):
                        window = limits.get(key)
                        if isinstance(window, dict):
                            windows.append({"label": window_label(window.get("windowDurationMins")),
                                            "used_percent": window.get("usedPercent"),
                                            "resets_at": window.get("resetsAt")})
                    if windows:
                        self.limit_windows = windows
                elif method == "thread/tokenUsage/updated":
                    count = params.get("tokenUsage", {}).get("last", {})
                    usage = TokenUsage(input_tokens=count.get("inputTokens", 0), output_tokens=count.get("outputTokens", 0))
                elif method == "turn/completed":
                    turn = params.get("turn", {})
                    if turn.get("status") != "completed":
                        error = turn.get("error") or {}
                        message = scrub(error.get("message", "") if isinstance(error, dict) else error)
                        limit = classify_limit(message)
                        if limit:
                            yield LimitHit(error_type=limit, message=message)
                            return
                        detail = f" Codex said: {message}" if message else ""
                        raise RuntimeError("Codex could not complete the turn. Check its login, model access, "
                                           f"or usage limit.{detail}")
                    yield AgentDone(usage=usage)
                    return
                elif method == "session/update":
                    update = params.get("update", {})
                    kind = update.get("sessionUpdate")
                    if kind == "agent_message_chunk" and update.get("content", {}).get("type") == "text":
                        yield TextDelta(content=update["content"].get("text", ""))
                    elif kind in {"tool_call", "tool_call_update"}:
                        yield NativeActivity(description=update.get("title", "Native tool activity"))
        finally:
            if event_task and not event_task.done():
                event_task.cancel()
            if event_task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await event_task
            await rpc.close()
