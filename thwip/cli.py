"""
Main interactive terminal CLI for thwip.

Universal coding agent multiplexer:
- Dynamic UI adapting to agent capabilities
- Seamless mid-conversation agent switching with context preservation
- Auto-detection of all installed tools & subscriptions
- Quota / rate limit exhaustion failover
- Rich live markdown and tool execution
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from thwip.agents import ALL_AGENT_CLASSES, AgentRegistry
from thwip.agents.base import (
    AgentDone,
    AgentEvent,
    BaseAgent,
    Capability,
    LimitHit,
    LimitStatus,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolResult,
    ToolUseStart,
)
from thwip.config import ThwipConfig, get_config_dir
from thwip.detector import SystemDetector
from thwip.limits import UsageTracker
from thwip.session import Session
from thwip.shortcuts import ThwipCompleter, create_keybindings
from thwip.theme import (
    console,
    get_brand,
    print_error,
    print_info,
    print_success,
    print_warning,
    render_agent_badge,
    render_agents_table,
    render_capability_disclaimer,
    render_dynamic_status_bar,
    render_limit_warning,
    render_markdown_response,
    render_startup_banner,
    render_user_prompt,
)
from thwip.tools import ToolManager
from thwip.utils import estimate_cost, format_cost, format_tokens


class ThwipCLI:
    """The interactive terminal CLI REPL."""

    def __init__(self) -> None:
        self.config = ThwipConfig.load()
        self.registry = AgentRegistry(self.config)
        self.detector = SystemDetector()
        self.usage_tracker = UsageTracker()
        self.tool_manager = ToolManager(self.config.project)
        self.session = Session(
            project_path=self.config.project,
            current_agent=self.config.default_agent,
            current_model=self.config.default_model,
        )
        self.current_agent = self._resolve_initial_agent()

    def _resolve_initial_agent(self) -> BaseAgent:
        agent = self.registry.get_agent(self.session.current_agent)
        if agent and agent.is_configured():
            return agent

        # Find first ready agent
        ready = self.registry.get_ready_agents()
        if ready:
            self.session.current_agent = ready[0].name
            self.session.current_model = ready[0].get_default_model()
            return ready[0]

        # Fallback to default instantiated agent even if missing key
        default = self.registry.get_agent("claude") or self.registry.list_agents()[0]
        return default

    def run(self) -> None:
        """Run the async CLI event loop."""
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        """Main async REPL loop."""
        # 1. Detect agents on system
        detected = self.detector.scan_all()
        ready_count = len(self.registry.get_ready_agents())

        # 2. Display startup banner
        console.clear()
        console.print(
            render_startup_banner(
                agent_name=self.current_agent.display_name,
                company=self.current_agent.company,
                model=self.session.current_model,
                project_path=os.path.abspath(self.session.project_path),
                session_name=self.session.name,
                agents_detected=len(detected),
                agents_ready=ready_count,
            )
        )

        console.print(
            "  [dim]Type [bold white]/help[/bold white] for commands, [bold white]/switch[/bold white] to change agent, or just start chatting.[/dim]\n"
        )

        # Setup prompt session
        history_file = str(get_config_dir() / "history.txt")
        prompt_session: PromptSession = PromptSession(
            history=FileHistory(history_file),
            completer=ThwipCompleter([a.name for a in self.registry.list_agents()]),
            key_bindings=create_keybindings(),
        )

        while True:
            try:
                # Dynamic prompt showing active agent brand
                brand = get_brand(self.current_agent.company)
                caps = [c.value for c in self.current_agent.capabilities]
                status_bar = render_dynamic_status_bar(
                    agent_name=self.current_agent.name,
                    company=self.current_agent.company,
                    model=self.session.current_model,
                    capabilities=caps,
                    tokens_used=self.session.get_total_tokens(),
                )

                console.print(status_bar)
                user_input = await prompt_session.prompt_async(
                    HTML(f"<b><style fg='{brand.primary}'>You ▶ </style></b>")
                )
                user_input = user_input.strip()

                if not user_input:
                    continue

                # Handle slash commands
                if user_input.startswith("/"):
                    handled = await self.handle_command(user_input)
                    if handled == "QUIT":
                        break
                    continue

                # Process chat message with agent
                await self.process_user_message(user_input)

            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Exiting thwip. Goodbye![/dim]")
                break
            except Exception as e:
                print_error(f"Unexpected error: {e}")

    async def handle_command(self, cmd_line: str) -> str | None:
        """Handle slash commands."""
        parts = cmd_line.split(" ", 2)
        cmd = parts[0].lower()
        arg1 = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if cmd in ("/quit", "/exit", "/q"):
            return "QUIT"

        elif cmd in ("/help", "/h"):
            self.show_help()

        elif cmd in ("/switch", "/s"):
            await self.cmd_switch(arg1, arg2)

        elif cmd in ("/agents", "/list"):
            self.cmd_show_agents()

        elif cmd in ("/models", "/m"):
            self.cmd_show_models(arg1)

        elif cmd == "/status":
            self.cmd_show_status()

        elif cmd == "/limits":
            self.cmd_show_limits()

        elif cmd == "/detect":
            self.cmd_detect()

        elif cmd == "/history":
            self.cmd_show_history()

        elif cmd == "/cost":
            self.cmd_show_cost()

        elif cmd == "/project":
            self.cmd_project(arg1)

        elif cmd == "/session":
            sub = arg1.lower()
            if sub == "save":
                path = self.session.save(arg2 or None)
                print_success(f"Session saved to {path.name}")
            elif sub == "load":
                loaded = Session.load(arg2)
                if loaded:
                    self.session = loaded
                    agent = self.registry.get_agent(loaded.current_agent)
                    if agent:
                        self.current_agent = agent
                    print_success(f"Loaded session '{loaded.name}' with {len(loaded.messages)} messages.")
                else:
                    print_error(f"Session '{arg2}' not found.")
            elif sub == "list":
                self.cmd_list_sessions()
            elif sub == "clear":
                self.session.messages.clear()
                print_info("Conversation history cleared.")
            else:
                print_info("Usage: /session [save|load|list|clear] [name]")

        else:
            print_warning(f"Unknown command '{cmd}'. Type /help for available commands.")

        return None

    def show_help(self) -> None:
        """Show help information."""
        table = Table(title="thwip Commands & Shortcuts", box=box.ROUNDED)
        table.add_column("Command / Key", style="bold cyan")
        table.add_column("Description", style="white")

        commands = [
            ("/switch [agent] [model]", "Switch active agent/model mid-conversation without losing context"),
            ("/agents", "Show all detected coding agents, company status & capabilities"),
            ("/models [agent]", "List available models for current or target agent"),
            ("/status", "Display current session, project, and token stats"),
            ("/limits", "View token usage, quota, and spend metrics"),
            ("/detect", "Re-scan system for newly installed coding agents"),
            ("/session save [name]", "Save current chat session"),
            ("/session load <name>", "Load a previously saved session"),
            ("/session list", "List all saved sessions"),
            ("/session clear", "Clear current conversation memory"),
            ("/history", "View conversation history with model attribution badges"),
            ("/cost", "Show estimated session and cumulative cost"),
            ("/project [path]", "View or change project working directory"),
            ("Ctrl + S", "Quick switch prompt"),
            ("Ctrl + T", "Status view"),
            ("Ctrl + H", "Show conversation history"),
            ("/quit", "Exit thwip"),
        ]
        for c, d in commands:
            table.add_row(c, d)
        console.print(table)

    async def cmd_switch(self, agent_name: str, model_id: str = "") -> None:
        """Switch to a different agent and/or model."""
        if not agent_name:
            # Interactive selection
            agents = self.registry.list_agents()
            console.print("\n[bold white]Select an agent to switch to:[/bold white]")
            for i, a in enumerate(agents, 1):
                brand = get_brand(a.company)
                status_str, _ = a.get_status_display()
                caps_str = ", ".join(c.value for c in a.capabilities)
                console.print(
                    f"  [bold white]{i}.[/bold white] [bold]{a.display_name}[/bold] "
                    f"({a.company}) - [{status_str}] - [dim]{caps_str}[/dim]"
                )

            choice = input(f"\nEnter choice [1-{len(agents)}]: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(agents):
                agent_name = agents[int(choice) - 1].name
            else:
                print_warning("Switch cancelled.")
                return

        new_agent = self.registry.get_agent(agent_name)
        if not new_agent:
            print_error(f"Unknown agent '{agent_name}'.")
            return

        old_agent = self.current_agent
        old_caps = set(old_agent.capabilities)

        self.current_agent = new_agent
        chosen_model = model_id or new_agent.get_default_model()
        self.session.switch_agent(new_agent.name, chosen_model)

        # Capability comparison & disclaimer
        missing = new_agent.get_missing_capabilities(old_caps)
        console.print(
            render_capability_disclaimer(
                agent_name=new_agent.display_name,
                company=new_agent.company,
                supported=[c.display_name for c in new_agent.capabilities],
                unsupported=missing,
            )
        )
        print_success(f"Now chatting with {new_agent.display_name} ({chosen_model}). Context preserved!")

    def cmd_show_agents(self) -> None:
        """Show table of all detected agents."""
        rows = [a.to_table_row() for a in self.registry.list_agents()]
        console.print(render_agents_table(rows))

    def cmd_show_models(self, agent_name: str = "") -> None:
        """List models for current or target agent."""
        target = self.registry.get_agent(agent_name) if agent_name else self.current_agent
        if not target:
            print_error(f"Agent '{agent_name}' not found.")
            return

        table = Table(title=f"Available Models for {target.display_name}", box=box.ROUNDED)
        table.add_column("Model ID", style="bold white")
        table.add_column("Name", style="cyan")
        table.add_column("Context", style="white")
        table.add_column("Tools", style="green")
        table.add_column("Thinking", style="magenta")
        table.add_column("Price (In/Out per 1M)", style="dim")

        for m in target.available_models:
            ctx = f"{m.context_window:,}" if m.context_window else "-"
            price = f"${m.pricing_input} / ${m.pricing_output}" if m.pricing_input else "Free / Local"
            table.add_row(
                m.id + (" (default)" if m.is_default else ""),
                m.name,
                ctx,
                "yes" if m.supports_tools else "-",
                "yes" if m.supports_thinking else "-",
                price,
            )
        console.print(table)

    def cmd_show_status(self) -> None:
        """Display status."""
        brand = get_brand(self.current_agent.company)
        content = Text()
        content.append(f"Agent:       {self.current_agent.display_name} ({self.current_agent.company})\n", style=brand.label_style)
        content.append(f"Model:       {self.session.current_model}\n", style="bold white")
        content.append(f"Project:     {os.path.abspath(self.session.project_path)}\n", style="white")
        content.append(f"Session:     {self.session.name} ({len(self.session.messages)} messages)\n", style="white")
        content.append(f"Tokens:      {self.session.get_total_tokens():,} used\n", style="dim")
        content.append(f"Config Key:  {self.config.key_sources.get(self.current_agent.name, 'None')}\n", style="dim")

        console.print(Panel(content, title="Current Status", box=box.ROUNDED))

    def cmd_show_limits(self) -> None:
        """Display usage metrics."""
        summary = self.usage_tracker.get_summary()
        table = Table(title="Usage & Limit Metrics", box=box.ROUNDED)
        table.add_column("Agent", style="bold white")
        table.add_column("Requests", style="cyan")
        table.add_column("Tokens (In / Out)", style="white")
        table.add_column("Est. Cost", style="green")
        table.add_column("Last Error / Limit", style="yellow")

        for agent_name, stats in summary.get("by_agent", {}).items():
            tok_str = f"{stats['input_tokens']:,} / {stats['output_tokens']:,}"
            table.add_row(
                agent_name,
                str(stats["request_count"]),
                tok_str,
                f"${stats['estimated_cost']:.4f}",
                stats.get("last_error") or "None",
            )
        console.print(table)

    def cmd_detect(self) -> None:
        """Re-scan system tools."""
        with console.status("[bold cyan]Scanning system for AI coding agents...[/bold cyan]"):
            detected = self.detector.scan_all()
        table = Table(title="Discovered Local Coding Agents and Extensions", box=box.ROUNDED)
        table.add_column("Name", style="bold white")
        table.add_column("Company", style="dim")
        table.add_column("Category", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Source", style="dim")

        for t in detected:
            status_style = "bold green" if t.is_configured else "bold yellow"
            table.add_row(
                t.name,
                t.company,
                t.category,
                f"[{status_style}]{t.subscription_status}[/{status_style}]",
                t.config_source or t.install_path,
            )
        console.print(table)

    def cmd_show_history(self) -> None:
        """Show conversation history with agent badges."""
        if not self.session.messages:
            print_info("No messages in current session.")
            return

        for m in self.session.messages:
            if m.role == "user":
                console.print(f"\n[bold cyan]You ▶ [/bold cyan]{m.content}")
            elif m.role == "assistant":
                badge = render_agent_badge(m.agent_name, m.model, m.company)
                console.print(f"\n", badge)
                console.print(render_markdown_response(m.content))

    def cmd_show_cost(self) -> None:
        summary = self.usage_tracker.get_summary()
        console.print(f"  [bold]Total Cumulative Spend:[/bold] [green]${summary['total_cost']:.4f}[/green]")
        console.print(f"  [bold]Total Requests:[/bold] {summary['total_requests']}")
        console.print(f"  [bold]Total Tokens Processed:[/bold] {summary['total_tokens']:,}")

    def cmd_project(self, new_path: str = "") -> None:
        if new_path:
            p = Path(new_path).expanduser().resolve()
            if p.is_dir():
                self.session.project_path = str(p)
                self.tool_manager = ToolManager(str(p))
                print_success(f"Project path changed to {p}")
            else:
                print_error(f"Directory '{new_path}' does not exist.")
        else:
            console.print(f"  Current project path: [bold white]{os.path.abspath(self.session.project_path)}[/bold white]")

    def cmd_list_sessions(self) -> None:
        sessions = Session.list_saved_sessions()
        if not sessions:
            print_info("No saved sessions found.")
            return
        table = Table(title="Saved Sessions", box=box.ROUNDED)
        table.add_column("Name", style="bold white")
        table.add_column("Agent", style="cyan")
        table.add_column("Model", style="white")
        table.add_column("Messages", style="green")
        table.add_column("Updated", style="dim")
        for s in sessions:
            table.add_row(s["name"], s["agent"], s["model"], str(s["messages_count"]), s["updated_at"])
        console.print(table)

    async def process_user_message(self, text: str) -> None:
        """Send message to active agent, handle streaming, tool calls, and limits."""
        self.session.add_user_message(text)

        # Prepare messages in portable format
        portable_msgs = self.session.to_portable_messages()

        brand = get_brand(self.current_agent.company)
        badge = render_agent_badge(
            self.current_agent.name,
            self.session.current_model,
            self.current_agent.company,
        )
        console.print(f"\n", badge)

        # Get tools if agent supports them
        tools = None
        if self.current_agent.has_capability(Capability.FILE_EDIT):
            if self.current_agent.name == "claude":
                tools = self.tool_manager.get_anthropic_tools()
            else:
                tools = self.tool_manager.get_openai_tools()

        collected_text = ""
        collected_thinking = ""
        total_tokens = 0
        limit_hit = False

        with Live(console=console, refresh_per_second=12) as live:
            try:
                stream = self.current_agent.chat(
                    messages=portable_msgs,
                    model=self.session.current_model,
                    system_prompt=self.session.system_prompt,
                    tools=tools,
                    stream=self.config.stream,
                )

                async for event in stream:
                    if isinstance(event, TextDelta):
                        collected_text += event.content
                        live.update(render_markdown_response(collected_text))

                    elif isinstance(event, ThinkingDelta):
                        collected_thinking += event.content
                        # Render thinking panel
                        think_panel = Panel(
                            Text(collected_thinking, style="dim italic"),
                            title="Reasoning / Thinking",
                            border_style="dim magenta",
                            box=box.MINIMAL,
                        )
                        live.update(think_panel)

                    elif isinstance(event, ToolUseStart):
                        live.stop()
                        console.print(
                            f"\n  [bold yellow]Action: {event.tool_name}[/bold yellow] [dim]{event.args}[/dim]"
                        )
                        # Execute tool
                        tool_output = self.tool_manager.execute_tool(event.tool_name, event.args)
                        console.print(f"  [dim green]Result:[/dim green] [dim]{tool_output[:200]}[/dim]")
                        live.start()

                    elif isinstance(event, AgentDone):
                        total_tokens = event.usage.input_tokens + event.usage.output_tokens
                        self.usage_tracker.record_usage(
                            agent_name=self.current_agent.name,
                            model=self.session.current_model,
                            input_tokens=event.usage.input_tokens,
                            output_tokens=event.usage.output_tokens,
                        )

                    elif isinstance(event, LimitHit):
                        limit_hit = True
                        live.stop()
                        self.usage_tracker.record_limit_hit(self.current_agent.name, event.message)
                        await self.handle_limit_failover(event)
                        break

            except Exception as e:
                live.stop()
                print_error(f"Agent error: {e}")

        if collected_text and not limit_hit:
            self.session.add_assistant_message(
                content=collected_text,
                agent_name=self.current_agent.name,
                model=self.session.current_model,
                company=self.current_agent.company,
                tokens=total_tokens,
            )

    async def handle_limit_failover(self, event: LimitHit) -> None:
        """Handle rate limit or quota exhaustion with auto-suggested failover."""
        # Find ready alternatives
        ready = self.registry.get_ready_agents()
        alternatives = []
        for a in ready:
            if a.name != self.current_agent.name:
                alternatives.append({
                    "agent": a.display_name,
                    "company": a.company,
                    "model": a.get_default_model(),
                    "capabilities": [c.value for c in a.capabilities],
                })

        console.print(
            render_limit_warning(
                agent_name=self.current_agent.display_name,
                company=self.current_agent.company,
                error_type=event.error_type.value,
                alternatives=alternatives,
            )
        )

        if not alternatives:
            print_warning("No other configured agents found. Please add an API key or start Ollama.")
            return

        choice = input("\nSwitch to alternative agent now? [1 to switch, Enter to cancel]: ").strip()
        if choice == "1" or choice.lower() == "y":
            target_alt = alternatives[0]
            for a in ready:
                if a.display_name == target_alt["agent"]:
                    await self.cmd_switch(a.name, target_alt["model"])
                    console.print("[bold green]Retrying your last message with new agent...[/bold green]")
                    last_msg = self.session.messages[-1].content
                    await self.process_user_message(last_msg)
                    break


def main() -> None:
    """Entry point for the thwip CLI command."""
    cli = ThwipCLI()
    cli.run()


if __name__ == "__main__":
    main()
