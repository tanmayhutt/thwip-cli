"""Run the evaluation harness: `python -m thwip.evals --provider fake` or `--provider all`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich import box
from rich.console import Console
from rich.table import Table

from thwip.config import ThwipConfig
from thwip.evals.fake_agent import FakeEvalAgent
from thwip.evals.runner import run_suite, summarize, write_report
from thwip.evals.tasks import TASKS, get_tasks


async def _agents_for(selection: str, project: str):
    if selection == "fake":
        return [FakeEvalAgent()]
    if selection == "broken":
        return [FakeEvalAgent(broken=True)]
    from thwip.agents import AgentRegistry

    registry = AgentRegistry(ThwipConfig.load())
    await registry.connect_native_agents(project)
    ready = registry.get_ready_agents()
    if selection == "all":
        return ready
    wanted = [a for a in ready if a.name == selection or registry.get_agent(selection) is a]
    if not wanted:
        names = ", ".join(a.name for a in ready) or "none"
        raise SystemExit(f"Provider '{selection}' is not ready. Ready providers: {names}")
    return wanted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m thwip.evals", description="Run thwip's evaluation tasks against providers.")
    parser.add_argument("--provider", default="fake", help="fake (offline, default), broken (offline failure demo), all, or a provider name")
    parser.add_argument("--task", action="append", help="Task id to run (repeatable). Default: all tasks")
    parser.add_argument("--list", action="store_true", help="List tasks and exit")
    parser.add_argument("--out", help="Write JSON results to this path")
    parser.add_argument("--project", default=".", help="Working directory for native CLIs")
    parser.add_argument("--memory", default="retrieve", choices=["retrieve", "truncate"],
                        help="How project memory is sent for memory tasks: retrieve (query-aware) or truncate (first 8,000 chars)")
    args = parser.parse_args(argv)
    console = Console()

    if args.list:
        table = Table(title="Evaluation tasks", box=box.ROUNDED)
        table.add_column("id", style="bold")
        table.add_column("kind")
        table.add_column("tests")
        table.add_column("touches", style="dim")
        for task in TASKS:
            table.add_row(task.id, task.kind, task.title, task.touches)
        console.print(table)
        return 0

    tasks = get_tasks(args.task)
    if not tasks:
        print("No tasks matched.", file=sys.stderr)
        return 2
    agents = asyncio.run(_agents_for(args.provider, args.project))
    results = asyncio.run(run_suite(agents, tasks, memory_mode=args.memory))

    table = Table(title=f"Results ({len(results)} runs)", box=box.ROUNDED)
    for column in ("task", "provider", "model", "pass", "latency s", "tokens in/out", "cost $", "tools", "memory chars", "note"):
        table.add_column(column, overflow="fold")
    for r in results:
        table.add_row(r.task_id, r.provider, r.model, "[green]pass[/green]" if r.passed else "[red]FAIL[/red]",
                      f"{r.latency_s:.2f}", f"{r.input_tokens}/{r.output_tokens}", f"{r.cost_usd:.4f}",
                      f"{r.tool_calls}" + (f" ({r.malformed_tool_calls} bad)" if r.malformed_tool_calls else ""),
                      f"{r.memory_chars} ({r.memory_mode})" if r.memory_chars else "-", r.note[:70])
    console.print(table)

    summary = summarize(results)
    totals = Table(title="Per provider", box=box.ROUNDED)
    for column in ("provider", "pass rate", "mean latency s", "cost $", "tool calls", "malformed", "errors"):
        totals.add_column(column)
    for name, row in summary.items():
        totals.add_row(name, f"{row['passed']}/{row['tasks']} ({row['pass_rate']:.0%})", f"{row['mean_latency_s']:.2f}",
                       f"{row['cost_usd']:.4f}", str(row["tool_calls"]), str(row["malformed_tool_calls"]), str(row["errors"]))
    console.print(totals)
    if args.out:
        console.print(f"[dim]Wrote {write_report(results, args.out)}[/dim]")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
