"""Native Codex and Gemini integrations using their supported stdio protocols."""

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
        self.name = provider
        self.binary = "codex" if provider == "openai" else "gemini"
        self.company = "OpenAI" if provider == "openai" else "Google"
        self.display_name = "Codex CLI" if provider == "openai" else "Gemini CLI"
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
        # Explicit IDs are resolved by the native provider, never rejected by a bundled catalog.
        if known or not model_id or not isinstance(model_id, str) or any(c.isspace() for c in model_id):
            return known
        return ModelInfo(id=model_id, name=model_id)

    async def _connect(self):
        executable = shutil.which(self.binary)
        if not executable:
            raise RuntimeError(f"{self.display_name} is no longer installed.")
        command = [executable, "app-server"] if self.name == "openai" else [executable, "--acp", "--approval-mode", "default"]
        rpc = await NativeRPC(command, self.project).start()
        try:
            if self.name == "openai":
                await rpc.request("initialize", {"clientInfo": {"name": "thwip", "version": __version__}})
                await rpc.send({"method": "initialized", "params": {}})
            else:
                await rpc.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                                 "clientInfo": {"name": "thwip", "version": __version__}})
            return rpc
        except BaseException:
            await rpc.close()
            raise

    async def refresh_models(self):
        rpc = None
        try:
            rpc = await self._connect()
            models = []
            if self.name == "openai":
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
            else:
                session = await rpc.request("session/new", {"cwd": self.project, "mcpServers": []}, timeout=45)
                info = session.get("models", {})
                for item in info.get("availableModels", []):
                    models.append(ModelInfo(id=item["modelId"], name=item.get("name", item["modelId"]),
                                            is_default=item["modelId"] == info.get("currentModelId")))
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
        prompt_task = None
        event_task = None
        try:
            # A fresh native session receives only portable text; tool state stays native within the turn.
            prompt = build_native_prompt(messages, None if self.name == "openai" else system_prompt)
            if self.name == "openai":
                # Codex App Server sandbox modes are kebab-case; camelCase is rejected as an invalid request.
                session = await rpc.request("thread/start", {
                    "cwd": self.project, "model": model or self.get_default_model(), "sandbox": "read-only",
                    "approvalPolicy": "on-request", "ephemeral": True,
                    **({"developerInstructions": system_prompt} if system_prompt else {}),
                })
                thread_id = session["thread"]["id"]
                await rpc.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})
            else:
                session = await rpc.request("session/new", {"cwd": self.project, "mcpServers": []}, timeout=45)
                session_id = session["sessionId"]
                await rpc.request("session/set_model", {"sessionId": session_id, "modelId": model or self.get_default_model()})
                prompt_task = asyncio.create_task(rpc.request("session/prompt", {
                    "sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]}, timeout=600))
            usage = TokenUsage()
            started_items = {}
            text_items = set()
            while True:
                if prompt_task and prompt_task.done() and rpc.events.empty():
                    result = prompt_task.result()
                    if result.get("stopReason") not in {"end_turn", "max_tokens"}:
                        raise RuntimeError("Gemini stopped before completing the response.")
                    yield AgentDone(usage=usage)
                    return
                event_task = asyncio.create_task(rpc.events.get())
                waiting = [event_task] + ([prompt_task] if prompt_task and not prompt_task.done() else [])
                done, _ = await asyncio.wait(waiting, timeout=600, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError("Native agent response timed out.")
                if event_task not in done:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                    continue
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
                    elif method == "session/request_permission":
                        permission = NativePermission(describe_permission(params.get("toolCall", {}), "Gemini"))
                        yield permission
                        kind = "allow_once" if permission.approved else "reject_once"
                        option = next((o for o in params.get("options", []) if o.get("kind") == kind), None)
                        outcome = {"outcome": "selected", "optionId": option["optionId"]} if option else {"outcome": "cancelled"}
                        await rpc.send({"id": event["id"], "result": {"outcome": outcome}})
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
            for task in (prompt_task, event_task):
                if task and not task.done():
                    task.cancel()
                if task:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            await rpc.close()
