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
    render_about_guide,
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

        elif cmd in ("/about", "/guide", "/info"):
            self.cmd_show_about()

        elif cmd in ("/help", "/h"):
            self.show_help()

        elif cmd in ("/switch", "/s"):
            await self.cmd_switch(arg1, arg2)

        elif cmd in ("/agents", "/list"):
            self.cmd_show_agents()

        elif cmd in ("/key", "/auth", "/config"):
            self.cmd_auth_config(arg1, arg2)

        elif cmd in ("/models", "/m"):
            self.cmd_show_models(arg1, arg2)

        elif cmd == "/tools":
            self.cmd_show_tools()

        elif cmd == "/status":
            self.cmd_show_status()

        elif cmd == "/limits":
            self.cmd_show_limits()

        elif cmd == "/detect":
            self.cmd_detect()

        elif cmd == "/history":
            self.cmd_show_history()

        elif cmd in ("/clear", "/reset"):
            self.session.messages.clear()
            print_info("Conversation history cleared.")

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
            print_warning(f"Unknown command '{cmd}'. Type /help or /about for navigation guide.")

        return None

    def cmd_show_about(self) -> None:
        """Display the complete About section and navigation guide."""
        detected = len(self.detector.scan_all())
        ready = len(self.registry.get_ready_agents())
        console.print(
            render_about_guide(
                agents_detected=detected,
                agents_ready=ready,
                active_agent=self.current_agent.display_name,
                active_model=self.session.current_model,
                active_company=self.current_agent.company,
            )
        )

    def cmd_show_tools(self) -> None:
        """Display available universal tools."""
        table = Table(title="Universal Tool Engine", box=box.ROUNDED)
        table.add_column("Tool Name", style="bold white")
        table.add_column("Category", style="cyan")
        table.add_column("Description", style="white")
        table.add_column("Status", style="green")

        tools_info = [
            ("read_file", "Filesystem", "Read file contents with line offset controls", "Active"),
            ("edit_file", "Filesystem", "Structured string replacement in local files", "Active"),
            ("write_file", "Filesystem", "Create or overwrite full files", "Active"),
            ("list_dir", "Filesystem", "List directory tree and metadata", "Active"),
            ("run_command", "Terminal", "Execute non-interactive shell commands safely", "Active"),
            ("run_code", "Sandbox", "Run inline Python or Node.js code snippets", "Active"),
            ("git_status", "Version Control", "Inspect repository changes and staging", "Active"),
            ("git_diff", "Version Control", "View unified unstaged/staged diffs", "Active"),
            ("git_commit", "Version Control", "Commit staged workspace changes", "Active"),
        ]
        for name, cat, desc, status in tools_info:
            table.add_row(name, cat, desc, f"[bold green]{status}[/bold green]")
        console.print(table)

    def show_help(self) -> None:
        """Show help information."""
        table = Table(title="thwip Commands & Shortcuts", box=box.ROUNDED)
        table.add_column("Command / Key", style="bold cyan")
        table.add_column("Description", style="white")

        commands = [
            ("/about", "Display full About section, architecture, and navigation guide"),
            ("/switch [agent] [model]", "Switch active agent/model mid-conversation without losing context"),
            ("/agents", "Show all detected coding agents, company status & capabilities"),
            ("/models [agent|tier]", "List models filtered by provider or tier (flagship, balanced, fast)"),
            ("/key [provider] [key]", "View or configure API keys saved in ~/.thwip/config.toml"),
            ("/tools", "List all universal file, terminal, and git tools"),
            ("/status", "Display current session, project, and token stats"),
            ("/limits", "View token usage, quota, and spend metrics"),
            ("/detect", "Re-scan system for newly installed coding agents"),
            ("/session save [name]", "Save current chat session"),
            ("/session load <name>", "Load a previously saved session"),
            ("/session list", "List all saved sessions"),
            ("/clear", "Clear current conversation memory"),
            ("/history", "View conversation history with model attribution badges"),
            ("/cost", "Show estimated session and cumulative cost"),
            ("/project [path]", "View or change project working directory"),
            ("Ctrl + S", "Interactive agent/model switcher prompt"),
            ("Ctrl + T", "Status view and token counters"),
            ("Ctrl + C", "Interrupt active response or tool execution"),
            ("/quit", "Exit thwip"),
        ]
        for c, d in commands:
            table.add_row(c, d)
        console.print(table)

    async def cmd_switch(self, agent_name: str, model_id: str = "") -> None:
        """Switch to a different agent and/or model."""
        if not agent_name:
            agents = self.registry.list_agents()
            console.print("\n[bold white]Available Coding Agents on Your Machine:[/bold white]")

            # Group: Ready / Configured
            ready_agents = [a for a in agents if a.is_configured()]
            installed_agents = [a for a in agents if a.is_installed() and not a.is_configured()]
            other_agents = [a for a in agents if not a.is_installed()]

            ordered = ready_agents + installed_agents + other_agents

            for i, a in enumerate(ordered, 1):
                brand = get_brand(a.company)
                status_str, status_style = a.get_status_display()
                install_info = a.get_install_info()
                loc = f" ({install_info['path']})" if install_info.get("path") else ""

                console.print(
                    f"  [bold white]{i}.[/bold white] [bold]{a.display_name}[/bold] "
                    f"({a.company}) - [{status_style}][{status_str}][/{status_style}][dim]{loc}[/dim]"
                )

            choice = input(f"\nEnter choice [1-{len(ordered)}]: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(ordered):
                agent_name = ordered[int(choice) - 1].name
            elif choice.lower() in [a.name for a in agents]:
                agent_name = choice.lower()
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

        if not new_agent.is_configured():
            key_name = {
                "google": "GEMINI_API_KEY",
                "openai": "OPENAI_API_KEY",
                "claude": "ANTHROPIC_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
                "groq": "GROQ_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
            }.get(new_agent.name, "API_KEY")
            print_warning(
                f"Notice: {new_agent.display_name} was selected, but no API key was found.\n"
                f"  Set your key with: export {key_name}=your_key_here\n"
                f"  Or add it to ~/.thwip/config.toml"
            )
        else:
            print_success(f"Now chatting with {new_agent.display_name} ({chosen_model}). Context preserved!")

    def cmd_show_agents(self) -> None:
        """Show table of all detected agents."""
        rows = [a.to_table_row() for a in self.registry.list_agents()]
        console.print(render_agents_table(rows))

    def cmd_show_models(self, arg1: str = "", arg2: str = "") -> None:
        """List models by provider or tier (flagship, balanced, fast)."""
        tier_filter = ""
        agent_target = None

        tier_aliases = {
            "flagship": "flagship",
            "high": "flagship",
            "pro": "flagship",
            "balanced": "balanced",
            "mid": "balanced",
            "flash": "balanced",
            "fast": "fast",
            "low": "fast",
            "lite": "fast",
            "mini": "fast",
        }

        if arg1.lower() in tier_aliases:
            tier_filter = tier_aliases[arg1.lower()]
        elif arg1:
            agent_target = self.registry.get_agent(arg1)
            if not agent_target:
                print_error(f"Agent '{arg1}' not found.")
                return
            if arg2.lower() in tier_aliases:
                tier_filter = tier_aliases[arg2.lower()]

        if not agent_target and not tier_filter:
            agent_target = self.current_agent

        if agent_target:
            agents_to_show = [agent_target]
            title = f"Available Models for {agent_target.display_name}"
            if tier_filter:
                title += f" ({tier_filter.title()} Tier)"
        else:
            agents_to_show = self.registry.list_agents()
            title = f"All {tier_filter.title()} Tier Models Across Providers"

        table = Table(title=title, box=box.ROUNDED)
        if not agent_target:
            table.add_column("Provider", style="cyan")
        table.add_column("Model ID", style="bold white")
        table.add_column("Name", style="white")
        table.add_column("Tier", style="bold")
        table.add_column("Context", style="dim")
        table.add_column("Thinking", style="magenta")
        table.add_column("Price (In/Out 1M)", style="green")

        tier_styles = {
            "flagship": "[bold magenta]Flagship / High[/bold magenta]",
            "balanced": "[bold cyan]Balanced / Mid[/bold cyan]",
            "fast": "[bold green]Fast / Low[/bold green]",
        }

        for ag in agents_to_show:
            for m in ag.available_models:
                m_tier = getattr(m, "tier", "balanced")
                if tier_filter and m_tier != tier_filter:
                    continue

                ctx = f"{m.context_window:,}" if m.context_window else "-"
                price = f"${m.pricing_input} / ${m.pricing_output}" if m.pricing_input else "Free"
                tier_badge = tier_styles.get(m_tier, m_tier.title())
                def_mark = " [dim](default)[/dim]" if m.is_default else ""

                row = []
                if not agent_target:
                    row.append(ag.company)
                row.extend([
                    m.id + def_mark,
                    m.name,
                    tier_badge,
                    ctx,
                    "yes" if m.supports_thinking else "-",
                    price,
                ])
                table.add_row(*row)

        console.print(table)
        console.print("[dim]Filter by tier: [bold]/models flagship[/bold], [bold]/models balanced[/bold], [bold]/models fast[/bold][/dim]\n")

    def cmd_auth_config(self, provider: str = "", key: str = "") -> None:
        """View or set API credentials stored in ~/.thwip/config.toml."""
        provider_map = {
            "google": "google",
            "gemini": "google",
            "antigravity": "google",
            "openai": "openai",
            "chatgpt": "openai",
            "codex": "openai",
            "claude": "anthropic",
            "anthropic": "anthropic",
            "deepseek": "deepseek",
            "groq": "groq",
            "openrouter": "openrouter",
        }

        if provider and key:
            target_prov = provider_map.get(provider.lower(), provider.lower())
            self.config.keys[target_prov] = key
            self.config.key_sources[target_prov] = "config.toml"
            self.config.save()

            # Refresh agent registry
            self.registry = AgentRegistry(self.config)
            agent = self.registry.get_agent(self.session.current_agent)
            if agent:
                self.current_agent = agent
            print_success(f"API key for '{target_prov}' saved to ~/.thwip/config.toml")
            return

        # Show interactive auth overview
        table = Table(title="thwip API Credentials & Authentication", box=box.ROUNDED)
        table.add_column("Provider", style="bold white")
        table.add_column("Auth Status", style="white")
        table.add_column("Key Source", style="dim")
        table.add_column("Config Command", style="cyan")

        for prov, label in [
            ("google", "Google Gemini / Antigravity"),
            ("openai", "OpenAI / ChatGPT / Codex"),
            ("anthropic", "Anthropic Claude"),
            ("deepseek", "DeepSeek"),
            ("groq", "Groq"),
            ("openrouter", "OpenRouter"),
        ]:
            has_key = bool(self.config.keys.get(prov))
            source = self.config.key_sources.get(prov, "none")
            status = "[bold green]Configured[/bold green]" if has_key else "[bold yellow]Missing[/bold yellow]"
            cmd_hint = f"/key {prov} <your_api_key>"
            table.add_row(label, status, source, cmd_hint)

        console.print(table)
        console.print("[dim]To set a key: [bold white]/key <provider> <key>[/bold white] (e.g. /key google AIzaSy...)[/dim]\n")

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
