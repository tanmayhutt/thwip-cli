"""Native Claude Code and Antigravity integrations through their stream-json print modes.

Both CLIs run one non-interactive turn per process using the sign-in already
stored by the CLI. Thwip never reads or copies those credentials. Tools that
would need an interactive approval are declined by the CLIs themselves in print
mode, so these connections behave as read-only assistants over the project.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from pathlib import Path

from thwip.agents.base import (
    AgentDone,
    BaseAgent,
    Capability,
    LimitHit,
    LimitStatus,
    ModelInfo,
    NativeActivity,
    SubscriptionInfo,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
)
from thwip.agents.native_common import build_incremental_prompt, classify_limit, network_hint, scrub
from thwip.tools.terminal import terminate_process_tree

TURN_TIMEOUT = 600
DISCOVERY_TIMEOUT = 60

# Claude Code resolves these aliases to the newest model of each family for the signed-in account.
CLAUDE_ALIASES = [
    ModelInfo(id="fable", name="Claude Fable (latest)", tier="flagship", supports_thinking=True, is_default=True),
    ModelInfo(id="opus", name="Claude Opus (latest)", tier="flagship", supports_thinking=True),
    ModelInfo(id="sonnet", name="Claude Sonnet (latest)", tier="balanced", supports_thinking=True),
    ModelInfo(id="haiku", name="Claude Haiku (latest)", tier="fast"),
]

PROVIDERS = {
    "claude": {"binary": "claude", "company": "Anthropic", "display_name": "Claude Code"},
    "google": {"binary": "agy", "company": "Google", "display_name": "Antigravity CLI"},
}


def _tier_for(model_id: str) -> str:
    lowered = model_id.lower()
    if "pro" in lowered or "opus" in lowered:
        return "flagship"
    if lowered.endswith("-low") or "lite" in lowered or "haiku" in lowered or "mini" in lowered:
        return "fast"
    return "balanced"


class PrintAgent(BaseAgent):
    """Runs an installed CLI in single-turn print mode with streamed JSON events."""

    native_tools = True
    capabilities = {Capability.CHAT, Capability.FILE_READ, Capability.FILE_EDIT,
                    Capability.CODE_RUN, Capability.TERMINAL, Capability.GIT}

    def __init__(self, provider: str, project: str):
        spec = PROVIDERS[provider]
        self.name = provider
        self.binary = spec["binary"]
        self.company = spec["company"]
        self.display_name = spec["display_name"]
        self.project = str(Path(project).expanduser().resolve())
        self.available_models: list[ModelInfo] = []
        self.ready = False
        self.discovery_error = "Not connected yet"
        self.limit_windows: list[dict] = []

    # --- Detection ---

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
        return {"method": "Native CLI print mode", "path": shutil.which(self.binary) or "", "version": ""}

    def get_subscription_info(self):
        return SubscriptionInfo(is_active=self.ready, message="Authentication and billing managed by installed CLI")

    def check_limits(self):
        return LimitStatus.OK if self.ready else LimitStatus.UNKNOWN

    def get_model_info(self, model_id):
        known = super().get_model_info(model_id)
        if known or not model_id or not isinstance(model_id, str) or any(c.isspace() for c in model_id):
            return known
        # Antigravity reports a live list, so only listed IDs are accepted. Claude Code has no list
        # endpoint: besides the aliases, accept full model names it documents (claude-...).
        if self.name == "claude" and model_id.startswith("claude-"):
            return ModelInfo(id=model_id, name=model_id, tier=_tier_for(model_id))
        return None

    # --- Process plumbing ---

    async def _start_process(self, command: list[str], stdin_text: str | None = None):
        executable = shutil.which(self.binary)
        if not executable:
            raise RuntimeError(f"{self.display_name} is no longer installed.")
        process = await asyncio.create_subprocess_exec(
            executable, *command, cwd=self.project,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024, start_new_session=os.name == "posix",
        )
        if stdin_text is not None:
            process.stdin.write(stdin_text.encode())
            await process.stdin.drain()
            process.stdin.close()
        return process

    async def _run_captured(self, command: list[str], timeout: float) -> tuple[int, str, str]:
        process = await self._start_process(command)
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            terminate_process_tree(process)
            await process.wait()
            raise
        return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    # --- Discovery ---

    async def refresh_models(self):
        try:
            models = await (self._discover_claude() if self.name == "claude" else self._discover_antigravity())
            if not models:
                raise RuntimeError("The installed CLI returned no selectable models.")
            self.available_models = list({model.id: model for model in models}.values())
            self.ready = True
            self.discovery_error = ""
        except (OSError, RuntimeError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            self.ready = False
            self.discovery_error = scrub(str(exc)) or "Native CLI discovery timed out."

    async def _discover_claude(self) -> list[ModelInfo]:
        code, out, _err = await self._run_captured(["auth", "status"], DISCOVERY_TIMEOUT)
        status = {}
        with contextlib.suppress(ValueError):
            status = json.loads(out or "{}")
        if code != 0 or not isinstance(status, dict) or not status.get("loggedIn"):
            raise RuntimeError("Claude Code has no active sign-in. Run `claude` once to log in.")
        return [ModelInfo(**vars(model)) for model in CLAUDE_ALIASES]

    async def _discover_antigravity(self) -> list[ModelInfo]:
        code, out, err = await self._run_captured(["models"], DISCOVERY_TIMEOUT)
        if code != 0:
            raise RuntimeError(f"Antigravity CLI could not list models: {scrub(err or out, 160) or 'unknown error'}")
        models = []
        for line in out.splitlines():
            if "\t" not in line:
                continue
            model_id, _, label = line.partition("\t")
            model_id = model_id.strip()
            if model_id:
                models.append(ModelInfo(id=model_id, name=label.strip() or model_id, tier=_tier_for(model_id),
                                        supports_thinking="thinking" in label.lower() or "high" in label.lower()))
        if models:
            models[0].is_default = True
        return models

    # --- Chat ---

    def _turn_command(self, prompt: str, model: str, system_prompt: str | None,
                      resume_id: str | None, new_id: str | None) -> tuple[list[str], str | None]:
        if self.name == "claude":
            command = ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                       "--model", model, "--allowedTools", "Read,Glob,Grep,LS,WebFetch,WebSearch"]
            command += ["--resume", resume_id] if resume_id else ["--session-id", new_id]
            if system_prompt and not resume_id:
                command += ["--append-system-prompt", system_prompt]
            return command, prompt
        command = ["--print", prompt, "--output-format", "stream-json", "--model", model,
                   "--print-timeout", f"{TURN_TIMEOUT}s"]
        if resume_id:
            command += ["--conversation", resume_id]
        return command, None

    async def chat(self, messages, model=None, system_prompt=None, tools=None, stream=True, resume=None):
        chosen = model or self.get_default_model()
        resume_id = resume.get("id") if isinstance(resume, dict) else None
        synced = resume.get("synced", 0) if isinstance(resume, dict) else 0
        if resume_id:
            prompt, _full = build_incremental_prompt(messages, None if self.name == "claude" else system_prompt, synced)
            try:
                async for event in self._turn(prompt, chosen, system_prompt, resume_id=resume_id, new_id=None):
                    yield event
                return
            except RuntimeError as exc:
                # The CLI could not continue that session (deleted, different machine, expired).
                yield NativeActivity(description=f"Previous {self.display_name} session unavailable "
                                                 f"({scrub(str(exc), 120)}); sending the full conversation to a new one.")
        prompt, _full = build_incremental_prompt(messages, None if self.name == "claude" else system_prompt, 0)
        async for event in self._turn(prompt, chosen, system_prompt, resume_id=None, new_id=str(uuid.uuid4())):
            yield event

    async def _turn(self, prompt, model, system_prompt, resume_id, new_id):
        command, stdin_text = self._turn_command(prompt, model, system_prompt, resume_id, new_id)
        process = await self._start_process(command, stdin_text)
        usage = TokenUsage()
        streamed_text = False
        finished = False
        session_id = resume_id or (new_id if self.name == "claude" else None)
        try:
            async for event in self._iterate(process):
                kind = event.get("type") or event.get("event")
                if kind == "system" and event.get("session_id"):
                    session_id = event["session_id"]
                elif kind == "init" and isinstance(event.get("init"), dict) or kind == "init":
                    session_id = event.get("conversation_id") or session_id
                if self.name == "claude":
                    result = self._claude_event(event, kind)
                else:
                    result = self._antigravity_event(event, kind)
                for item in result:
                    if isinstance(item, TokenUsage):
                        usage = item
                    elif isinstance(item, str):
                        if item and not streamed_text:
                            yield TextDelta(content=item)
                    elif isinstance(item, AgentDone):
                        finished = True
                        yield AgentDone(usage=usage, native_session={"id": session_id} if session_id else {})
                        return
                    else:
                        if isinstance(item, TextDelta):
                            streamed_text = True
                        yield item
                        if isinstance(item, LimitHit):
                            return
            await process.wait()
            stderr = ""
            if process.stderr:
                stderr = scrub((await process.stderr.read(4096)).decode("utf-8", "replace"), 200)
            if not finished:
                limit = classify_limit(stderr)
                if limit:
                    yield LimitHit(error_type=limit, message=stderr)
                    return
                detail = f" {stderr}" if stderr else ""
                raise RuntimeError(f"{self.display_name} exited before completing the response "
                                   f"(exit code {process.returncode}).{detail}{network_hint(stderr)}")
        finally:
            if process.returncode is None:
                terminate_process_tree(process)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), 5)

    async def _iterate(self, process):
        deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"{self.display_name} response timed out.")
            line = await asyncio.wait_for(process.stdout.readline(), remaining)
            if not line:
                return
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event

    def _claude_event(self, event: dict, kind: str) -> list:
        if kind == "stream_event":
            inner = event.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    return [TextDelta(content=delta.get("text", ""))]
                if delta.get("type") == "thinking_delta":
                    return [ThinkingDelta(content=delta.get("thinking", ""))]
            return []
        if kind == "assistant":
            content = event.get("message", {}).get("content", [])
            return [NativeActivity(description=f"Tool: {block.get('name', 'tool')} {scrub(json.dumps(block.get('input', {})), 160)}")
                    for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info", {}) if isinstance(event.get("rate_limit_info"), dict) else {}
            windows = info.get("unifiedWindows", {}) if isinstance(info.get("unifiedWindows"), dict) else {}
            labels = {"five_hour": "5h", "seven_day": "7d"}
            reported = []
            for key, window in windows.items():
                if key not in labels:
                    continue
                if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
                    reported.append({"label": labels.get(key, key), "used_percent": window["utilization"] * 100,
                                     "resets_at": window.get("resetsAt")})
            if reported:
                self.limit_windows = reported
            if info.get("status") == "rejected":
                return [LimitHit(error_type=LimitStatus.QUOTA_EXHAUSTED,
                                 message="Claude Code reported that the account usage limit was reached.")]
            return []
        if kind == "result":
            raw_usage = event.get("usage", {}) if isinstance(event.get("usage"), dict) else {}
            usage = TokenUsage(input_tokens=int(raw_usage.get("input_tokens", 0) or 0)
                               + int(raw_usage.get("cache_read_input_tokens", 0) or 0)
                               + int(raw_usage.get("cache_creation_input_tokens", 0) or 0),
                               output_tokens=int(raw_usage.get("output_tokens", 0) or 0))
            text = event.get("result", "") if isinstance(event.get("result"), str) else ""
            if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
                message = scrub(text or event.get("subtype", "unknown error"))
                limit = classify_limit(message)
                if limit:
                    return [LimitHit(error_type=limit, message=message)]
                raise RuntimeError(f"Claude Code could not complete the turn: {message}{network_hint(message)}")
            return [usage, text, AgentDone()]
        return []

    def _antigravity_event(self, event: dict, kind: str) -> list:
        if kind == "step_update":
            step = event.get("step_update", {})
            step_type = step.get("step_type", "")
            if step_type == "agent_response":
                return [TextDelta(content=step.get("text_delta", ""))] if step.get("text_delta") else []
            if step_type in {"user_input", "thinking"}:
                return [ThinkingDelta(content=step["text_delta"])] if step_type == "thinking" and step.get("text_delta") else []
            if step.get("state") == "ACTIVE":
                return [NativeActivity(description=f"Tool: {step_type}")]
            return []
        if kind == "result":
            result = event.get("result", {})
            raw_usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}
            usage = TokenUsage(input_tokens=int(raw_usage.get("input_tokens", 0) or 0),
                               output_tokens=int(raw_usage.get("output_tokens", 0) or 0))
            if result.get("status") != "SUCCESS":
                message = scrub(result.get("error") or result.get("status") or "unknown error")
                limit = classify_limit(message)
                if limit:
                    return [LimitHit(error_type=limit, message=message)]
                raise RuntimeError(f"Antigravity CLI could not complete the turn: {message}{network_hint(message)}")
            text = result.get("response", "") if isinstance(result.get("response"), str) else ""
            return [usage, text.rstrip("\n"), AgentDone()]
        return []
