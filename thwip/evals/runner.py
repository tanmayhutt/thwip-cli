"""Run tasks against an adapter the same way the REPL would, and score them.

For a "model" task the runner: builds a throwaway project from the fixture, sends the prompt
(with thwip's tool definitions when the task asks for tools), executes any tool the model
requests through ToolManager, feeds the results back, and stops when the model answers without
asking for more tools. It records latency, tokens, estimated cost, tool calls and malformed
output, then applies the task's check.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from thwip.agents.base import AgentDone, LimitHit, NativeActivity, TextDelta, ToolUseStart
from thwip.evals.tasks import EvalTask, Observation
from thwip.memory import ProjectMemory
from thwip.tools import ToolManager
from thwip.utils import estimate_cost

MAX_TOOL_ROUNDS = 4


@dataclass
class EvalResult:
    task_id: str
    provider: str
    model: str
    passed: bool
    note: str = ""
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    native_activity: int = 0
    malformed_tool_calls: int = 0
    rounds: int = 0
    error: str = ""
    answer: str = ""          # first 200 characters of the final text, for transparency
    memory_chars: int = 0     # characters of project memory sent (0 when the task carries none)
    memory_mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _materialize(fixture: dict[str, str]) -> Path:
    """Create the task's files in a fresh temp folder. Paths like ../x land beside the project on purpose."""
    base = Path(tempfile.mkdtemp(prefix="thwip-eval-"))
    project = base / "project"
    project.mkdir()
    for relative, content in fixture.items():
        target = (project / relative).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return project


async def run_task(agent, task: EvalTask, model: str | None = None, memory_mode: str = "retrieve") -> EvalResult:
    """memory_mode is "retrieve" (query-aware sections) or "truncate" (the first 8,000 characters), for before/after runs."""
    chosen = model or agent.get_default_model()
    result = EvalResult(task_id=task.id, provider=agent.name, model=chosen, passed=False, memory_mode=memory_mode if task.use_memory else "")
    project = _materialize(task.fixture)
    manager = ToolManager(str(project))
    observation = Observation()
    started = time.perf_counter()
    # Native CLIs resolve files against their own working directory, so point them at the fixture.
    previous_project = getattr(agent, "project", None)
    if previous_project is not None:
        agent.project = str(project)
    try:
        if task.kind == "system":
            result.passed, result.note = task.check(observation, {"tool_manager": manager})
            return result
        tools = None
        if task.tools:
            tools = manager.get_anthropic_tools() if agent.name == "claude" and not getattr(agent, "native_tools", False) \
                else manager.get_openai_tools()
        if getattr(agent, "native_tools", False):
            tools = None  # native CLIs bring their own tools; we can only observe their final text
        system_prompt = task.system_prompt
        if task.use_memory:
            injection = ProjectMemory(str(project)).injection(task.prompt, mode=memory_mode)
            system_prompt = f"{system_prompt}\n\n{injection}".strip()
            result.memory_chars = len(injection)
        messages: list[dict[str, Any]] = [*task.history, {"role": "user", "content": task.prompt}]
        for round_index in range(MAX_TOOL_ROUNDS):
            result.rounds = round_index + 1
            requests: list[ToolUseStart] = []
            round_text = ""
            native_state: dict = {}
            async for event in agent.chat(messages=messages, model=chosen, system_prompt=system_prompt,
                                          tools=tools, stream=tools is None):
                observation.events.append(event)
                if isinstance(event, TextDelta):
                    round_text += event.content
                elif isinstance(event, ToolUseStart):
                    requests.append(event)
                    if not isinstance(event.args, dict):
                        result.malformed_tool_calls += 1
                elif isinstance(event, NativeActivity):
                    result.native_activity += 1
                elif isinstance(event, AgentDone):
                    result.input_tokens += event.usage.input_tokens
                    result.output_tokens += event.usage.output_tokens
                    native_state = event.native_state
                elif isinstance(event, LimitHit):
                    result.error = f"limit: {event.message[:160]}"
            observation.text += round_text
            if result.error or not requests:
                break
            tool_messages = []
            for request in requests:
                observation.tool_calls.append(request)
                output = manager.execute_tool(request.tool_name, request.args)
                if output.startswith(("Error: Unknown tool", "Error: Missing", "Error: Argument", "Error: Unknown argument")):
                    result.malformed_tool_calls += 1
                observation.tool_outputs.append(output)
                tool_messages.append({"role": "tool", "tool_call_id": request.tool_id, "name": request.tool_name, "content": output})
            messages.append({"role": "assistant", "content": round_text, "tool_calls": [
                {"id": r.tool_id, "type": "function", "function": {"name": r.tool_name, "arguments": r.args}} for r in requests],
                **({"_native_state": native_state} if native_state else {})})
            messages.extend(tool_messages)
        else:
            result.error = result.error or f"stopped after {MAX_TOOL_ROUNDS} tool rounds"
        result.tool_calls = len(observation.tool_calls)
        if not getattr(agent, "native_tools", False):
            result.cost_usd = round(estimate_cost(chosen, result.input_tokens, result.output_tokens), 6)
        result.answer = observation.text.strip()[:200]
        if result.error:
            result.passed, result.note = False, result.error
        else:
            result.passed, result.note = task.check(observation, {"tool_manager": manager,
                                                                  "native": bool(getattr(agent, "native_tools", False))})
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        result.passed, result.note = False, result.error
    finally:
        result.latency_s = round(time.perf_counter() - started, 3)
        if previous_project is not None:
            closer = getattr(agent, "close", None)
            if callable(closer):
                # A live Codex thread is bound to the fixture folder; drop it before the folder goes away.
                try:
                    await closer()
                except Exception:
                    pass
            agent.project = previous_project
        shutil.rmtree(project.parent, ignore_errors=True)
    return result


async def run_suite(agents: list, tasks: list[EvalTask], memory_mode: str = "retrieve") -> list[EvalResult]:
    results: list[EvalResult] = []
    system_tasks = [t for t in tasks if t.kind == "system"]
    model_tasks = [t for t in tasks if t.kind != "system"]
    for task in system_tasks:
        probe = agents[0] if agents else _SystemOnly()
        results.append(await run_task(probe, task))
    try:
        for agent in agents:
            for task in model_tasks:
                results.append(await run_task(agent, task, memory_mode=memory_mode))
    finally:
        # Native adapters keep a CLI process alive between turns; never leave it running after a run.
        for agent in agents:
            closer = getattr(agent, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    pass
    return results


class _SystemOnly:
    """Stand-in 'provider' so system tasks can run without any adapter."""
    name = "system"
    native_tools = False

    def get_default_model(self):
        return "-"


def summarize(results: list[EvalResult]) -> dict[str, dict[str, Any]]:
    """Per-provider totals: pass rate, mean latency, cost, tool calls, malformed outputs."""
    summary: dict[str, dict[str, Any]] = {}
    for item in results:
        row = summary.setdefault(item.provider, {"tasks": 0, "passed": 0, "latency_s": 0.0, "cost_usd": 0.0,
                                                 "tool_calls": 0, "malformed_tool_calls": 0, "errors": 0})
        row["tasks"] += 1
        row["passed"] += int(item.passed)
        row["latency_s"] += item.latency_s
        row["cost_usd"] += item.cost_usd
        row["tool_calls"] += item.tool_calls
        row["malformed_tool_calls"] += item.malformed_tool_calls
        row["errors"] += int(bool(item.error))
    for row in summary.values():
        row["pass_rate"] = round(row["passed"] / row["tasks"], 3) if row["tasks"] else 0.0
        row["mean_latency_s"] = round(row["latency_s"] / row["tasks"], 3) if row["tasks"] else 0.0
        row["cost_usd"] = round(row["cost_usd"], 6)
        del row["latency_s"]
    return summary


def write_report(results: list[EvalResult], path: str | Path) -> Path:
    """Save raw results plus the summary as JSON; the benchmark page is built from this file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": [r.to_dict() for r in results],
               "summary": summarize(results)}
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target
