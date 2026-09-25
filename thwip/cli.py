"""
Main interactive terminal CLI for thwip.

Universal coding agent multiplexer:
- Dynamic UI adapting to agent capabilities
- Seamless mid-conversation agent switching with context preservation
- Detection of supported installed tools and available credentials
- Quota / rate limit exhaustion failover
- Rich live markdown and tool execution
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich import box
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from thwip.agents import AgentRegistry
from thwip.agents.base import (
    AgentDone,
    BaseAgent,
    Capability,
    LimitHit,
    NativeActivity,
    NativePermission,
    TextDelta,
    ThinkingDelta,
    ToolUseStart,
)
from thwip.agents.native_common import describe_limit_windows
from thwip.config import DisplayConfig, ThwipConfig, get_config_dir
from thwip.detector import SystemDetector
from thwip.handoff import build_handoff_report, local_capabilities, local_model
from thwip.limits import UsageTracker
from thwip.memory import ProjectMemory, Vault, detect_obsidian_vaults
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
)
from thwip.tools import ToolManager

# prompt_toolkit's cursor-position query is answered by the terminal with an escape sequence.
# During a long native turn nobody reads stdin, so the answer would be echoed as ^[ ... R and later
# swallowed as input. The layout does not need it, so disable the query.
os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")


class QuietTerminal:
    """Turn off keyboard echo while a turn runs and drop stray input before the next prompt."""

    def __enter__(self):
        self._saved = None
        try:
            import termios
            self._fd = sys.stdin.fileno()
            if sys.stdin.isatty():
                self._saved = termios.tcgetattr(self._fd)
                quiet = termios.tcgetattr(self._fd)
                quiet[3] &= ~termios.ECHO
                termios.tcsetattr(self._fd, termios.TCSANOW, quiet)
        except (ImportError, OSError, ValueError, AttributeError):
            self._saved = None
        return self

    def __exit__(self, *exc):
        try:
            import termios
            if self._saved is not None:
                termios.tcflush(self._fd, termios.TCIFLUSH)
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved)
        except (ImportError, OSError, ValueError, AttributeError):
            pass
        return False


class TurnInterrupted(Exception):
    """Raised when the user presses Ctrl+C at a prompt shown during a turn."""


class SafeFileHistory(FileHistory):
    """Keep inline API-key commands out of persistent prompt history."""

    def store_string(self, string: str) -> None:
        if re.match(r"^\s*/(?:key|auth|config|k)\s+\S+\s+\S+", string, re.IGNORECASE):
            return
        super().store_string(string)


class ThwipCLI:
    """The interactive terminal CLI REPL."""

    def __init__(self, project: str | None = None) -> None:
        self.config = ThwipConfig.load()
        if project:
            self.config.project = project
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
        console.width = min(console.width, self.config.display.max_width)
        # Plain/light terminals avoid hard-coded bright provider colors.
        console.no_color = self.config.theme in {"plain", "light"}

    def _render_response(self, content: str):
        display = getattr(self.config, "display", DisplayConfig())
        return render_markdown_response(content, display.markdown, display.syntax_highlight,
                                        getattr(self.config, "theme", "dark") == "light")

    def _render_status(self):
        display = getattr(self.config, "display", DisplayConfig())
        return render_dynamic_status_bar(
            agent_name=self.current_agent.name,
            company=self.current_agent.company if display.dynamic_ui else "",
            model=self.session.current_model,
            capabilities=[cap.value for cap in self.current_agent.get_capabilities_for_model(self.session.current_model)],
            tokens_used=self.session.get_total_tokens() if display.show_token_count else 0,
            cost=self.usage_tracker.get_summary()["total_cost"] if display.show_cost else 0,
            show_agent=display.show_agent_badge,
            show_capabilities=display.show_capabilities,
        )

    def _resolve_initial_agent(self) -> BaseAgent:
        agent = self.registry.get_agent(self.session.current_agent)
        if agent and agent.is_configured():
            if not agent.get_model_info(self.session.current_model):
                self.session.current_model = agent.get_default_model()
            return agent

        # Find first ready agent
        ready = self.registry.get_ready_agents()
        if ready:
            self.session.current_agent = ready[0].name
            self.session.current_model = ready[0].get_default_model()
            return ready[0]

        # Prefer an actually installed agent even when it still needs an API key.
        installed = [agent for agent in self.registry.list_agents() if agent.is_installed()]
        default = installed[0] if installed else (self.registry.get_agent("claude") or self.registry.list_agents()[0])
        self.session.current_agent = default.name
        self.session.current_model = default.get_default_model()
        return default

    def run(self) -> None:
        """Run the async CLI event loop."""
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        """Main async REPL loop."""
        print_info("Connecting installed agents using their existing sign-ins...")
        await self.registry.connect_native_agents(str(Path(self.session.project_path).resolve()))
        self.current_agent = self._resolve_initial_agent()
        if getattr(self.current_agent, "native_tools", False):
            self.session.current_model = self.current_agent.get_default_model()
        # 1. Detect agents on system
        self.detector.scan_all()
        all_agents = self.registry.list_agents()
        installed_count = len([a for a in all_agents if a.is_installed()])
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
                agents_detected=installed_count,
                agents_ready=ready_count,
            )
        )

        console.print(
            "  [dim]Type [bold white]/help[/bold white] for commands, [bold white]/switch[/bold white] to change agent, or just start chatting.[/dim]\n"
        )
        await self._memory_onboarding()

        # Setup prompt session
        history_path = get_config_dir() / "history.txt"
        history_path.touch(mode=0o600, exist_ok=True)
        try:
            history_path.chmod(0o600)
        except OSError:
            pass
        history_file = str(history_path)
        # With cursor-position queries disabled, prompt_toolkit would print blank lines to reserve room for
        # the completion menu; let it scroll instead.
        prompt_session: PromptSession = PromptSession(
            history=SafeFileHistory(history_file),
            completer=ThwipCompleter([a.name for a in self.registry.list_agents()]),
            key_bindings=create_keybindings(),
            reserve_space_for_menu=0,
        )

        while True:
            try:
                # Dynamic prompt showing active agent brand
                brand = get_brand(self.current_agent.company)
                console.print(self._render_status())
                user_input = await prompt_session.prompt_async(
                    HTML(f"<b><style fg='{brand.primary}'>You ▶ </style></b>")
                )
                user_input = user_input.strip()

                if not user_input:
                    continue

                # Shell escape, as in Claude Code: run a command in the project without involving a model.
                if user_input.startswith("!"):
                    await self._run_interruptible(self.cmd_shell(user_input[1:].strip()))
                    continue

                # Handle slash commands
                if user_input.startswith("/"):
                    handled = await self._run_interruptible(self.handle_command(user_input))
                    if handled == "QUIT":
                        await self._run_interruptible(self._offer_memory_update("quitting"))
                        break
                    continue

                # Process chat message with agent; Ctrl+C interrupts the turn, not the REPL.
                with QuietTerminal():
                    await self._run_interruptible(self.process_user_message(self._expand_mentions(user_input)))

            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Exiting thwip. Goodbye![/dim]")
                break
            except Exception as e:
                print_error(f"Unexpected error: {e}")
        await self._close_native_agents()

    async def _close_native_agents(self) -> None:
        for agent in self.registry.list_agents():
            closer = getattr(agent, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    await closer()

    async def _ask_text(self, question: str) -> str:
        """Ask a one-line question without blocking the event loop.

        Ctrl+C here interrupts the current turn or command instead of exiting Thwip.
        """
        try:
            answer = await PromptSession(reserve_space_for_menu=0).prompt_async(HTML(f"<b>{question}</b> "))
        except (KeyboardInterrupt, EOFError):
            raise TurnInterrupted from None
        return answer.strip()

    async def _ask_yes_no(self, question: str) -> bool:
        return (await self._ask_text(question)).lower() in {"y", "yes"}

    def _discard_interrupted_turn(self) -> None:
        console.print()
        if self.session.messages and self.session.messages[-1].role == "user":
            self.session.messages.pop()
            print_warning("Response interrupted. The unanswered message was removed; send it again or /switch.")
        else:
            print_warning("Interrupted.")

    async def _run_interruptible(self, coroutine):
        """Run one turn so that Ctrl+C cancels the turn and its child processes only."""
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(coroutine)
        interrupted = False

        def on_interrupt() -> None:
            nonlocal interrupted
            interrupted = True
            task.cancel()

        previous = signal.getsignal(signal.SIGINT)
        try:
            loop.add_signal_handler(signal.SIGINT, on_interrupt)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                return await task
            except TurnInterrupted:
                self._discard_interrupted_turn()
            return None
        try:
            return await task
        except asyncio.CancelledError:
            if not interrupted:
                raise
            self._discard_interrupted_turn()
        except TurnInterrupted:
            self._discard_interrupted_turn()
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)
        return None

    async def handle_command(self, cmd_line: str) -> str | None:
        """Handle slash commands."""
        parts = cmd_line.split(maxsplit=2)
        if not parts:
            return None
        cmd = parts[0].lower()
        arg1 = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if cmd in ("/quit", "/exit", "/q"):
            return "QUIT"

        elif cmd in ("/about", "/guide", "/info", "/g"):
            self.cmd_show_about()

        elif cmd in ("/help", "/h"):
            self.show_help()

        elif cmd in ("/switch", "/s", "/sw"):
            await self.cmd_switch(arg1, arg2)

        elif cmd == "/handoff":
            self.cmd_handoff(arg1, arg2)

        elif cmd == "/native":
            await self.cmd_native(arg1, arg2)

        elif cmd in ("/agents", "/list", "/a"):
            self.cmd_show_agents()

        elif cmd in ("/key", "/auth", "/config", "/k"):
            await self.cmd_auth_config(arg1, arg2)

        elif cmd in ("/models", "/m"):
            target = self.registry.get_agent(arg1) if arg1 else self.current_agent
            if target and (getattr(target, "native_tools", False) or target.is_configured()):
                if getattr(target, "native_tools", False):
                    target.project = str(Path(self.session.project_path).resolve())
                with console.status("[dim]Refreshing model list...[/dim]"):
                    await target.refresh_models()
                if target.discovery_error:
                    print_warning(target.discovery_error)
            self.cmd_show_models(arg1, arg2)

        elif cmd in ("/tools", "/t"):
            self.cmd_show_tools()

        elif cmd == "/status":
            self.cmd_show_status()

        elif cmd in ("/limits", "/usage"):
            self.cmd_show_limits()

        elif cmd == "/model":
            await self.cmd_model(arg1)

        elif cmd == "/new":
            self.cmd_new()

        elif cmd == "/resume":
            await self.cmd_resume(arg1)

        elif cmd == "/compact":
            await self.cmd_compact()

        elif cmd == "/diff":
            self.cmd_diff(arg1)

        elif cmd == "/copy":
            self.cmd_copy()

        elif cmd == "/export":
            self.cmd_export(cmd_line.partition(" ")[2].strip())

        elif cmd in ("/memory", "/mem", "/brain"):
            await self.cmd_memory(arg1.lower(), cmd_line.partition(" ")[2].partition(" ")[2].strip())

        elif cmd == "/detect":
            await self.registry.connect_native_agents(str(Path(self.session.project_path).resolve()))
            self.current_agent = self.registry.get_agent(self.session.current_agent) or self.current_agent
            self.cmd_detect()

        elif cmd == "/history":
            self.cmd_show_history()

        elif cmd in ("/clear", "/reset"):
            self.session.clear_context()
            print_info("Conversation history cleared.")

        elif cmd == "/cost":
            self.cmd_show_cost()

        elif cmd == "/project":
            path_arg = cmd_line.partition(" ")[2].strip()
            if len(path_arg) >= 2 and path_arg[0] == path_arg[-1] and path_arg[0] in "\"'":
                path_arg = path_arg[1:-1]
            self.cmd_project(path_arg)

        elif cmd == "/session":
            sub = arg1.lower()
            if sub == "save":
                path = self.session.save(arg2 or None)
                print_success(f"Session saved to {path.name}")
            elif sub == "load":
                self._load_session(arg2)
            elif sub == "list":
                self.cmd_list_sessions()
            elif sub == "clear":
                self.session.clear_context()
                print_info("Conversation history cleared.")
            else:
                print_info("Usage: /session [save|load|list|clear] [name]")

        else:
            print_warning(f"Unknown command '{cmd}'. Type /help or /about for navigation guide.")

        return None

    def _load_session(self, name: str) -> bool:
        loaded = Session.load(name) if name else None
        if not loaded:
            print_error(f"Session '{name}' not found.")
            return False
        project = Path(loaded.project_path).expanduser().resolve()
        if not project.is_dir():
            print_error(f"Saved project directory '{loaded.project_path}' no longer exists.")
            return False
        agent = self.registry.get_agent(loaded.current_agent)
        if not agent:
            print_error(f"Saved agent '{loaded.current_agent}' is not available.")
            return False
        if not agent.get_model_info(loaded.current_model):
            loaded.current_model = agent.get_default_model()
            print_warning("The saved model is unavailable. Using the provider default.")
        self.session = loaded
        self.current_agent = agent
        self.tool_manager = ToolManager(str(project))
        print_success(f"Loaded session '{loaded.name}' with {len(loaded.messages)} messages.")
        return True

    async def cmd_model(self, model_id: str = "") -> None:
        """Pick a model for the current agent, interactively or by ID, like /model in Codex and Claude Code."""
        agent = self.current_agent
        models = list(agent.available_models)
        if not model_id:
            if not models:
                print_info("No models are listed for this agent yet. Use /models to refresh.")
                return
            console.print(f"\n[bold white]Models for {agent.display_name}:[/bold white]")
            for index, model in enumerate(models, 1):
                marker = " [dim](current)[/dim]" if model.id == self.session.current_model else ""
                console.print(f"  [bold white]{index}.[/bold white] {model.id} [dim]{model.name} | {model.tier}[/dim]{marker}")
            answer = await self._ask_text(f"Choose [1-{len(models)}] or type a model ID (Enter to cancel):")
            if not answer:
                print_info("Model unchanged.")
                return
            model_id = models[int(answer) - 1].id if answer.isdigit() and 1 <= int(answer) <= len(models) else answer
        if not agent.get_model_info(model_id):
            print_error(f"Unknown model '{model_id}' for {agent.display_name}. Use /models to see the list.")
            return
        if model_id not in {model.id for model in models}:
            print_warning(f"'{model_id}' is not in the list reported by {agent.display_name}; the provider validates it on your next message.")
        self.session.switch_agent(agent.name, model_id)
        print_success(f"Model set to {model_id} on {agent.display_name}. Conversation preserved.")

    def cmd_new(self) -> None:
        """Start a fresh conversation, saving the current one first when it has content."""
        if self.session.messages and getattr(self.config, "auto_save", False):
            saved = self.session.save()
            print_info(f"Previous conversation saved as {saved.stem}. Use /resume to return to it.")
        self.session = Session(project_path=self.session.project_path, current_agent=self.current_agent.name,
                               current_model=self.session.current_model)
        print_success(f"Started new session {self.session.name}.")

    async def cmd_resume(self, name: str = "") -> None:
        """Resume a saved session by name or from a numbered list, like /resume in Codex and Claude Code."""
        if name:
            self._load_session(name)
            return
        sessions = Session.list_saved_sessions()
        if not sessions:
            print_info("No saved sessions found.")
            return
        console.print("\n[bold white]Saved sessions:[/bold white]")
        for index, item in enumerate(sessions, 1):
            console.print(f"  [bold white]{index}.[/bold white] {item['name']} [dim]{item['agent']}/{item['model']} | "
                          f"{item['messages_count']} messages | {item['updated_at']}[/dim]")
        answer = await self._ask_text(f"Resume [1-{len(sessions)}] (Enter to cancel):")
        if answer.isdigit() and 1 <= int(answer) <= len(sessions):
            self._load_session(sessions[int(answer) - 1]["name"])
        elif answer:
            self._load_session(answer)
        else:
            print_info("Nothing resumed.")

    async def cmd_compact(self) -> None:
        """Replace the conversation with a summary written by the current model.

        The summary is plain text, so it stays portable across providers and keeps
        a later /switch or /handoff small.
        """
        portable = self.session.to_portable_messages()
        if len(portable) < 2:
            print_info("Nothing to compact yet.")
            return
        if not self.current_agent.is_configured():
            print_error(f"{self.current_agent.display_name} is not connected; /switch to a ready agent first.")
            return
        transcript = "\n\n".join(f"[{m['role'].title()}]\n{m['content']}" for m in portable)
        request = ("Summarize the conversation below for another assistant that will continue it. Keep every decision, "
                   "requirement, file path, command, error, and open task. Use short bullet points under the headings "
                   "Context, Decisions, Open tasks. Do not add commentary.\n\n" + transcript)
        summary = ""
        with console.status("[dim]Compacting conversation...[/dim]"):
            try:
                stream = self.current_agent.chat(messages=[{"role": "user", "content": request}],
                                                 model=self.session.current_model,
                                                 system_prompt="You write faithful, compact summaries.", tools=None, stream=False)
                async with contextlib.aclosing(stream) if hasattr(stream, "aclose") else contextlib.nullcontext(stream) as stream:
                    async for event in stream:
                        if isinstance(event, TextDelta):
                            summary += event.content
                        elif isinstance(event, LimitHit):
                            print_warning(f"Provider limit while compacting: {event.message}")
                            return
            except TurnInterrupted:
                raise
            except Exception as exc:
                print_error(f"Compaction failed: {exc}")
                return
        summary = summary.strip()
        if not summary:
            print_error("The model returned an empty summary; conversation unchanged.")
            return
        before = len(self.session.messages)
        self.session.clear_context()
        self.session.add_user_message("Summary of the conversation so far, compacted by thwip:\n\n" + summary)
        self.session.add_assistant_message("Understood. I will continue from this summary.",
                                           agent_name=self.current_agent.name, model=self.session.current_model,
                                           company=self.current_agent.company)
        print_success(f"Compacted {before} messages into a summary. /history shows it; /handoff shows the new size.")

    def cmd_diff(self, arg: str = "") -> None:
        """Show the project's git diff, like /diff in Codex."""
        from rich.syntax import Syntax

        from thwip.tools.git_ops import GitOps

        git = GitOps(self.session.project_path)
        staged = arg.lower() in {"--staged", "staged", "--cached"}
        output = git.diff(staged=staged)
        if output.startswith(("Git error", "Git operation failed")):
            print_error(output)
            return
        if output == "Success." or not output.strip():
            print_info("No staged changes." if staged else "Working tree clean (use /diff staged for the index).")
            return
        limit = 20000
        console.print(Syntax(output[:limit], "diff", theme="ansi_dark", word_wrap=False))
        if len(output) > limit:
            print_info(f"Diff truncated to {limit:,} characters.")

    def cmd_copy(self) -> None:
        """Copy the last assistant response to the clipboard, like /copy in Claude Code."""
        import subprocess

        last = next((m for m in reversed(self.session.messages) if m.role == "assistant"), None)
        if not last:
            print_info("No assistant response to copy yet.")
            return
        commands = [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
        for command in commands:
            if shutil.which(command[0]):
                try:
                    subprocess.run(command, input=last.content.encode(), check=True, timeout=5)
                except (OSError, subprocess.SubprocessError) as exc:
                    print_error(f"Clipboard command failed: {exc}")
                    return
                print_success(f"Copied {len(last.content):,} characters from {last.model or last.agent_name}.")
                return
        print_error("No clipboard tool found (pbcopy, wl-copy, xclip, or xsel).")

    def cmd_export(self, target: str = "") -> None:
        """Write the conversation as Markdown with model attribution, for sharing or handing to another tool."""
        if not self.session.messages:
            print_info("Nothing to export yet.")
            return
        path = Path(target).expanduser() if target else Path(self.session.project_path) / f"{self.session.name}.md"
        if not path.is_absolute():
            path = Path(self.session.project_path) / path
        lines = [f"# thwip conversation {self.session.name}", "",
                 f"Project: {os.path.abspath(self.session.project_path)}  ", f"Exported: {time.strftime('%Y-%m-%d %H:%M')}", ""]
        for message in self.session.messages:
            if message.role == "user":
                lines += ["## You", "", message.content, ""]
            elif message.role == "assistant":
                who = " / ".join(part for part in (message.company, message.model or message.agent_name) if part)
                lines += [f"## Assistant ({who})", "", message.content, ""]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            print_error(f"Could not write export: {exc}")
            return
        print_success(f"Exported {len(self.session.messages)} messages to {path}")

    async def cmd_shell(self, command: str) -> None:
        """Run a shell command in the project directory and show its output; nothing is sent to a model."""
        from thwip.tools.terminal import TerminalRunner

        if not command:
            print_info("Usage: !<command>  (runs in the project directory)")
            return
        runner = TerminalRunner(self.session.project_path)
        output = await runner.run_command_async(command, timeout=120)
        console.print(Text(output[:20000] or "(no output)"))
        if len(output) > 20000:
            print_info("Output truncated to 20,000 characters.")

    def _expand_mentions(self, text: str) -> str:
        """Attach files referenced as @path (inside the project) to the message, like @ mentions in Codex and Claude Code."""
        mentions = re.findall(r"(?<!\S)@([\w./\-]+)", text)
        if not mentions:
            return text
        attachments = []
        project = Path(self.session.project_path).resolve()
        for mention in dict.fromkeys(mentions):
            candidate = (project / mention).resolve()
            if not candidate.is_file() or project not in candidate.parents:
                continue
            content = self.tool_manager.execute_tool("read_file", {"file_path": mention, "max_lines": 400})
            attachments.append(f"[Attached file: {mention}]\n{content}")
            print_info(f"Attached {mention}")
        if not attachments:
            return text
        return text + "\n\n" + "\n\n".join(attachments)

    # --- Project memory and second brain ---

    def _memory_config(self):
        from thwip.config import MemoryConfig
        return getattr(self.config, "memory", None) or MemoryConfig()

    def _memory(self) -> ProjectMemory:
        return ProjectMemory(self.session.project_path, self._memory_config().file)

    def _vault(self) -> Vault | None:
        cfg = self._memory_config()
        return Vault(cfg.vault, getattr(cfg, "cards_dir", "Projects")) if cfg.vault else None

    def _system_prompt_with_memory(self) -> str:
        """Base instructions plus the project's memory file, so every provider shares the same project state."""
        base = self.session.system_prompt or ""
        if not self._memory_config().enabled:
            return base
        injection = self._memory().injection()
        return f"{base}\n\n{injection}".strip() if injection else base

    async def _memory_onboarding(self) -> None:
        """First run: connect a second-brain vault. Detected Obsidian vaults are offered; a new one can be created."""
        cfg = self._memory_config()
        if not cfg.enabled or cfg.onboarded or not sys.stdin.isatty():
            return
        vaults = detect_obsidian_vaults()
        default_new = str(Path.home() / "thwip-brain")
        console.print(Panel(Text(
            "thwip keeps one memory file per project (context.md) that every agent you use reads and helps maintain.\n"
            "It can also file each project into a second-brain vault (an Obsidian vault or any Markdown folder), where\n"
            "projects that share a stack, area, or tag link to each other. thwip only writes notes it created itself."),
            title="Second brain", box=box.ROUNDED))
        options = [f"Use existing Obsidian vault: {path}" for path in vaults] + [f"Create a new vault at {default_new}", "Skip for now"]
        for index, option in enumerate(options, 1):
            console.print(f"  [bold white]{index}.[/bold white] {option}")
        try:
            answer = await self._ask_text(f"Choose [1-{len(options)}] or type a folder path:")
        except TurnInterrupted:
            answer = ""
        chosen = ""
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            index = int(answer) - 1
            if index < len(vaults):
                chosen = vaults[index]
            elif index == len(vaults):
                chosen = default_new
        elif answer:
            chosen = answer
        if chosen:
            vault = Vault(chosen)
            try:
                vault.create()
            except OSError as exc:
                print_error(f"Could not create the vault folder: {exc}")
                chosen = ""
        cfg.vault = chosen
        cfg.onboarded = True
        with contextlib.suppress(Exception):
            self.config.save()
        if chosen:
            print_success(f"Second brain connected: {chosen}. Use /memory to view or update this project's memory.")
        else:
            print_info("No vault connected. Project memory still works locally; set one later with /memory vault <path>.")

    async def cmd_memory(self, sub: str = "", rest: str = "") -> None:
        """Project memory: show, init, edit, update (model-proposed, confirmed), sync (vault), link, vault."""
        cfg = self._memory_config()
        memory = self._memory()
        if sub in ("", "show"):
            if not memory.exists():
                print_info(f"No {cfg.file} in this project yet. /memory init creates one; /memory update fills it from this conversation.")
                return
            from thwip.memory import parse_frontmatter, render_frontmatter
            data, body = parse_frontmatter(memory.read())
            if data:
                console.print(Text(render_frontmatter(data), style="dim"))
            console.print(self._render_response(body))
            vault = self._vault()
            console.print(Text(f"File: {memory.path}" + (f"  |  Vault: {vault.root}" if vault else "  |  No vault connected (/memory vault <path>)"), style="dim"))
        elif sub == "init":
            if memory.exists():
                print_info(f"{cfg.file} already exists.")
                return
            area = rest or "General"
            memory.init(area=area)
            print_success(f"Created {memory.path} from the template with detected facts. Edit it or run /memory update.")
            self._sync_vault(memory)
        elif sub == "edit":
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
            if not editor:
                print_error("Set $EDITOR (or $VISUAL) to use /memory edit.")
                return
            if not memory.exists():
                memory.init()
            import shlex
            import subprocess
            await asyncio.to_thread(subprocess.call, [*shlex.split(editor), str(memory.path)])
            self._sync_vault(memory)
        elif sub == "update":
            await self._memory_update()
        elif sub == "sync":
            if rest.strip() in {"all", "--all"}:
                self._sync_all()
                return
            if not memory.exists():
                print_info("Nothing to file yet; create the memory with /memory init first.")
                return
            self._sync_vault(memory, verbose=True)
        elif sub == "vault":
            if not rest:
                vault = self._vault()
                print_info(f"Vault: {vault.root}" if vault else "No vault connected. Usage: /memory vault <path>")
                return
            vault = Vault(rest)
            try:
                vault.create()
            except OSError as exc:
                print_error(f"Could not use that folder: {exc}")
                return
            cfg.vault = str(vault.root)
            cfg.onboarded = True
            self.config.save()
            print_success(f"Vault set to {vault.root}")
            if memory.exists():
                self._sync_vault(memory, verbose=True)
        elif sub == "link":
            self._memory_link()
        else:
            print_info("Usage: /memory [show|init [area]|edit|update|sync [all]|vault <path>|link]")

    def _sync_all(self) -> None:
        """File every project under the configured scan folders and rebuild the cross-project links."""
        cfg = self._memory_config()
        vault = self._vault()
        if not vault:
            print_info("No vault connected; /memory vault <path> to connect one.")
            return
        roots = list(getattr(cfg, "scan", [])) or [str(Path(self.session.project_path).resolve().parent)]
        memories = Vault.discover_projects(roots, cfg.file)
        if not memories:
            print_info(f"No projects with {cfg.file} found under: {', '.join(roots)}")
            return
        try:
            report = vault.sync_all(memories)
        except OSError as exc:
            print_error(f"Vault sync failed: {exc}")
            return
        print_success(f"Filed {report['projects']} projects from {', '.join(roots)}: {len(report['written'])} notes written or refreshed "
                      f"under {vault.cards_root} plus Stack, Areas, and Tags hubs.")
        for path in report["skipped"]:
            print_warning(f"Left untouched (not created by thwip): {path}")

    def _sync_vault(self, memory: ProjectMemory, verbose: bool = False) -> None:
        vault = self._vault()
        if not vault:
            if verbose:
                print_info("No vault connected; /memory vault <path> to connect one.")
            return
        try:
            report = vault.sync(memory)
        except OSError as exc:
            print_error(f"Vault sync failed: {exc}")
            return
        if verbose or report["written"]:
            written = len(report["written"])
            print_success(f"Filed in the vault: {written} note{'s' if written != 1 else ''} written or refreshed under {vault.root}.")
        for path in report["skipped"]:
            print_warning(f"Left untouched (not created by thwip): {path}")

    def _memory_link(self) -> None:
        """Point the CLIs' own instruction files at the memory file so they read it outside thwip as well."""
        cfg = self._memory_config()
        project = Path(self.session.project_path).resolve()
        line = f"\nProject memory: read `{cfg.file}` in this directory before working; update only durable facts in it.\n"
        for name in ("AGENTS.md", "CLAUDE.md"):
            target = project / name
            existing = target.read_text(encoding="utf-8") if target.is_file() else ""
            if cfg.file in existing:
                print_info(f"{name} already references {cfg.file}.")
                continue
            target.write_text(existing.rstrip("\n") + ("\n" if existing else "") + line, encoding="utf-8")
            print_success(f"Added a pointer to {cfg.file} in {name}.")

    async def _memory_update(self, reason: str = "") -> bool:
        """Ask the current model to revise the memory file from this conversation; write only after confirmation."""
        import difflib

        memory = self._memory()
        portable = self.session.to_portable_messages()
        if not portable:
            print_info("No conversation to record yet.")
            return False
        if not self.current_agent.is_configured():
            print_error(f"{self.current_agent.display_name} is not connected; /switch to a ready agent first.")
            return False
        if not memory.exists():
            memory.init()
            print_info(f"Created {memory.path} from the template first.")
        transcript = "\n\n".join(f"[{m['role'].title()}]\n{m['content']}" for m in portable)
        proposed = ""
        with console.status("[dim]Asking the model to update the project memory...[/dim]"):
            try:
                stream = self.current_agent.chat(messages=[{"role": "user", "content": memory.update_prompt(transcript)}],
                                                 model=self.session.current_model,
                                                 system_prompt="You maintain concise, factual project memory files.", tools=None, stream=False)
                async with contextlib.aclosing(stream) if hasattr(stream, "aclose") else contextlib.nullcontext(stream) as stream:
                    async for event in stream:
                        if isinstance(event, TextDelta):
                            proposed += event.content
                        elif isinstance(event, LimitHit):
                            print_warning(f"Provider limit while updating memory: {event.message}")
                            return False
            except TurnInterrupted:
                raise
            except Exception as exc:
                print_error(f"Memory update failed: {exc}")
                return False
        proposed = proposed.strip()
        if proposed.startswith("```"):
            proposed = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", proposed).strip()
        if not proposed.startswith("---") or "## Current Work" not in proposed:
            print_error("The model did not return a valid memory file; nothing written.")
            return False
        current = memory.read()
        proposed = memory.touch_updated(proposed)
        diff = list(difflib.unified_diff(current.splitlines(), proposed.splitlines(), "current", "proposed", lineterm=""))
        if not diff:
            print_info("The model proposed no changes to the project memory.")
            return False
        from rich.syntax import Syntax
        console.print(Syntax("\n".join(diff[:400]), "diff", theme="ansi_dark", word_wrap=False))
        if len(diff) > 400:
            print_info("Diff truncated for display; the full file is written if you accept.")
        if not await self._ask_yes_no(f"Write these changes to {memory.filename}? [y/N]"):
            print_info("Project memory unchanged.")
            return False
        memory.write(proposed)
        print_success(f"Project memory updated: {memory.path}")
        self._sync_vault(memory)
        return True

    async def _offer_memory_update(self, reason: str) -> None:
        cfg = self._memory_config()
        if not cfg.enabled or not cfg.offer_update_on_quit or len(self.session.to_portable_messages()) < 2:
            return
        if not sys.stdin.isatty() or not self.current_agent.is_configured():
            return
        if await self._ask_yes_no(f"Update this project's memory ({cfg.file}) from the conversation before {reason}? [y/N]"):
            await self._memory_update(reason)

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
        if getattr(self.current_agent, "native_tools", False):
            print_info("Tools are provided by the connected CLI. Its tool activity and permission requests appear during responses.")
            return
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
            ("list_files", "Filesystem", "List project files", "Active"),
            ("run_command", "Terminal", "Execute confirmed shell commands in the project", "Active"),
            ("run_python", "Execution", "Run confirmed Python snippets with local user permissions", "Active"),
            ("git_status", "Version Control", "Inspect repository changes and staging", "Active"),
            ("git_diff", "Version Control", "View unified unstaged/staged diffs", "Active"),
        ]
        for name, cat, desc, status in tools_info:
            table.add_row(name, cat, desc, f"[bold green]{status}[/bold green]")
        console.print(table)

    async def cmd_native(self, provider: str, extra: str = "") -> None:
        """Replace this REPL with the native CLI without copying its credentials."""
        if provider != "codex" or extra:
            print_info("Usage: /native codex. Other native providers are not supported yet.")
            return
        executable = shutil.which("codex")
        if not executable:
            print_error("Codex CLI is not installed or not on PATH. Install it before using /native codex.")
            return
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print_error("Native Codex requires an interactive terminal.")
            return
        project = Path(self.session.project_path).expanduser().resolve()
        if not project.is_dir():
            print_error("The current project directory no longer exists. Set it with /project first.")
            return
        print_info(
            "This exits Thwip and opens Codex in the current project with a read-only sandbox "
            "and on-request approvals. Codex uses its own authentication, model, and usage limits. "
            "Your Thwip session will be saved, but its conversation is not transferred. "
            "Restart Thwip and use /session load to return."
        )
        if not await self._ask_yes_no("Open native Codex? [y/N]"):
            return
        try:
            saved = self.session.save()
            print_info(f"Thwip session saved: {saved}")
            sys.stdout.flush()
            sys.stderr.flush()
            os.execv(executable, [executable, "--cd", str(project), "--sandbox", "read-only",
                                  "--ask-for-approval", "on-request"])
        except OSError:
            print_error("Could not save the session or start Codex. Thwip remains open; check file permissions and installation.")

    def show_help(self) -> None:
        """Show help information."""
        table = Table(title="thwip Commands & Shortcuts", box=box.ROUNDED)
        table.add_column("Command / Key", style="bold cyan")
        table.add_column("Description", style="white")

        commands = [
            ("/about", "Display full About section, architecture, and navigation guide"),
            ("/switch [agent] [model]", "Switch provider with a text-continuity report"),
            ("/handoff [agent] [model]", "Preview transfer losses and context pressure without switching"),
            ("/native codex", "Save and leave Thwip for Codex using its own login; no context transfer"),
            ("/agents", "Show all detected coding agents, company status & capabilities"),
            ("/models [agent|tier]", "List models filtered by provider or tier (flagship, balanced, fast)"),
            ("/key [provider]", "Securely enter an API key without storing it in terminal history"),
            ("/tools", "List all universal file, terminal, and git tools"),
            ("/status", "Display current session, project, and token stats"),
            ("/limits", "View token usage, quota, and spend metrics"),
            ("/detect", "Re-scan system for newly installed coding agents"),
            ("/model [id]", "Pick a model for the current agent (interactive list or ID)"),
            ("/new", "Start a fresh conversation (current one is saved first)"),
            ("/resume [name]", "Resume a saved session from a numbered list"),
            ("/compact", "Summarize the conversation with the current model to free context"),
            ("/diff [staged]", "Show the project's git diff"),
            ("/copy", "Copy the last response to the clipboard"),
            ("/export [path]", "Write the conversation to a Markdown file with model attribution"),
            ("!<command>", "Run a shell command in the project without a model"),
            ("@path in a message", "Attach a project file's content to your message"),
            ("/memory [show|init|edit|update|sync|vault|link]", "Project memory file shared by every agent, filed into your second-brain vault"),
            ("/session save [name]", "Save current chat session"),
            ("/session load <name>", "Load a previously saved session"),
            ("/session list", "List all saved sessions"),
            ("/clear", "Clear current conversation memory"),
            ("/history", "View conversation history with model attribution badges"),
            ("/cost", "Show estimated session and cumulative cost"),
            ("/project [path]", "View or change project working directory"),
            ("Ctrl + S", "Interactive agent/model switcher prompt"),
            ("Ctrl + T", "Status view and token counters"),
            ("Ctrl + C", "Interrupt the current response, command, or prompt"),
            ("/quit", "Exit thwip"),
        ]
        for c, d in commands:
            table.add_row(c, d)
        console.print(table)

    def cmd_handoff(self, agent_name: str = "", model_id: str = "") -> None:
        """Preview any catalogued target without requiring credentials or API calls."""
        target = self.registry.get_agent(agent_name) if agent_name else self.current_agent
        if target is None:
            print_error(f"Unknown agent '{agent_name}'.")
            return
        models = target.get_handoff_models()
        default = next((model.id for model in models if model.is_default), models[0].id if models else "")
        chosen = model_id or (self.session.current_model if not agent_name else default)
        if local_model(target, chosen) is None:
            print_error(f"Unknown model '{chosen}' for {target.display_name}.")
            return
        caps = local_capabilities(target, chosen)
        tools = None
        if Capability.FILE_EDIT in caps:
            tools = (
                self.tool_manager.get_anthropic_tools() if target.name == "claude"
                else self.tool_manager.get_openai_tools()
            )
        report = build_handoff_report(self.session, self.current_agent, target, chosen, tools)
        table = Table(title="Handoff Preview (local only)", box=box.ROUNDED)
        table.add_column("Check", style="cyan")
        table.add_column("Result")
        rows = [
            ("Route", f"{report.source} -> {report.target}"),
            ("Preserved", f"{report.transferred_messages} user/assistant text messages + system prompt"),
            ("Excluded records", (f"{report.excluded_messages} non-text-history messages; "
             f"{report.excluded_tool_calls} stored tool-call entries")),
            ("Transient results", f"{report.observed_tool_results} observed tool results not transferred"),
            ("Tracking coverage", "Since session creation" if report.tracking_complete
             else "Partial: legacy session has uncounted earlier tool results"),
            ("Capabilities lost", ", ".join(report.lost_capabilities) or "None in local catalog"),
            ("Capabilities gained", ", ".join(report.gained_capabilities) or "None in local catalog"),
            ("Context pressure", (f"{report.context_pressure}: ~{report.estimated_input_tokens:,} input "
             f"+ {report.output_reserve:,} advisory output reserve / "
             f"{report.context_window or 'unknown'} catalog tokens")),
            ("Text SHA-256", report.text_fingerprint),
        ]
        for label, value in rows:
            table.add_row(Text(label), Text(value))
        console.print(table)
        console.print(Text(
            "Advisory only: token estimates and catalog limits can differ from provider behavior. "
            "Hidden reasoning and provider-native state do not transfer. Working files stay on disk; "
            "they are not uploaded by this preview. The fingerprint checks text equality, not delivery. "
            "No model calls, trimming, or switching were performed by the preview.", style="dim",
        ))

    async def cmd_switch(self, agent_name: str, model_id: str = "") -> None:
        """Switch to a different agent and/or model."""
        if not agent_name:
            all_agents = self.registry.list_agents()
            installed = [a for a in all_agents if a.is_installed()]

            if not installed:
                print_error("No agents detected on your machine.")
                return

            console.print("\n[bold white]Available Coding Agents on Your Machine:[/bold white]")

            # Sort: configured first, then just installed
            ready_agents = [a for a in installed if a.is_configured()]
            unready_agents = [a for a in installed if not a.is_configured()]
            ordered = ready_agents + unready_agents

            for i, a in enumerate(ordered, 1):
                status_str, status_style = a.get_status_display()
                install_info = a.get_install_info()
                loc = f" ({install_info['path']})" if install_info.get("path") else ""

                console.print(
                    f"  [bold white]{i}.[/bold white] [bold]{a.display_name}[/bold] "
                    f"({a.company}) - [{status_style}][{status_str}][/{status_style}][dim]{loc}[/dim]"
                )

            not_installed = len(all_agents) - len(installed)
            if not_installed > 0:
                console.print(f"\n  [dim]{not_installed} other providers available (DeepSeek, Groq, Ollama, OpenRouter). Use /agents to see all.[/dim]")

            choice = await self._ask_text(f"Enter choice [1-{len(ordered)}]:")
            if choice.isdigit() and 1 <= int(choice) <= len(ordered):
                agent_name = ordered[int(choice) - 1].name
            elif choice.lower() in [a.name for a in installed]:
                agent_name = choice.lower()
            else:
                print_warning("Switch cancelled.")
                return

        new_agent = self.registry.get_agent(agent_name)
        if not new_agent:
            print_error(f"Unknown agent '{agent_name}'.")
            return

        if not new_agent.is_installed():
            print_error(f"{new_agent.display_name} is not installed on this machine.")
            return

        if getattr(new_agent, "native_tools", False) and not new_agent.is_configured():
            new_agent.project = str(Path(self.session.project_path).resolve())
            await new_agent.refresh_models()
            if not new_agent.is_configured():
                print_error(new_agent.discovery_error)
                return

        chosen_model = model_id or new_agent.get_default_model()
        if not new_agent.get_model_info(chosen_model):
            valid_models = ", ".join(model.id for model in new_agent.available_models)
            print_error(f"Unknown model '{chosen_model}' for {new_agent.display_name}. Available: {valid_models}")
            return
        if chosen_model not in {model.id for model in new_agent.available_models}:
            print_warning(f"'{chosen_model}' is not in the catalog reported by {new_agent.display_name}. "
                          "The CLI validates it on your next message; use /models to see the reported list.")

        old_agent = self.current_agent
        old_caps = old_agent.get_capabilities_for_model(self.session.current_model)

        self.cmd_handoff(new_agent.name, chosen_model)

        self.current_agent = new_agent
        self.session.switch_agent(new_agent.name, chosen_model)

        # Capability comparison & disclaimer
        new_capabilities = new_agent.get_capabilities_for_model(chosen_model)
        missing = [
            capability.display_name
            for capability in sorted(old_caps - new_capabilities, key=lambda item: item.value)
        ]
        console.print(
            render_capability_disclaimer(
                agent_name=new_agent.display_name,
                company=new_agent.company,
                supported=[c.display_name for c in new_capabilities],
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
        elif getattr(new_agent, "native_tools", False):
            print_success(
                f"Now chatting with {new_agent.display_name} ({chosen_model}) through its existing sign-in. "
                "Portable text history preserved."
            )
        else:
            print_success(
                f"Now chatting with {new_agent.display_name} ({chosen_model}). "
                "Portable text history preserved."
            )

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
            if not agent_target.is_installed():
                print_error(f"{agent_target.display_name} is not installed or configured on this machine.")
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
            if getattr(agent_target, "native_tools", False):
                title += " (reported by the CLI)"
            elif getattr(agent_target, "catalog_source", "bundled") == "live":
                title += " (live from provider)"
            elif agent_target.name != "ollama":
                title += " (bundled fallback; add a key for the live list)"
        else:
            agents_to_show = [agent for agent in self.registry.list_agents() if agent.is_installed()]
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
                if getattr(ag, "native_tools", False):
                    price = "CLI account"
                elif m.pricing_input or m.pricing_output:
                    price = f"${m.pricing_input} / ${m.pricing_output}"
                elif ag.name == "ollama":
                    price = "Free (local)"
                else:
                    price = "See provider"
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

    async def _rebuild_registry(self) -> None:
        """Reload adapters after a credential change and keep native connections for the rest."""
        self.registry = AgentRegistry(self.config)
        await self.registry.connect_native_agents(str(Path(self.session.project_path).resolve()))
        agent = self.registry.get_agent(self.session.current_agent)
        if agent:
            self.current_agent = agent
            if not agent.get_model_info(self.session.current_model):
                self.session.current_model = agent.get_default_model()

    async def cmd_auth_config(self, provider: str = "", key: str = "") -> None:
        """View or set API credentials stored in ~/.thwip/config.toml."""
        provider_map = {
            "1": "google",
            "google": "google",
            "gemini": "google",
            "antigravity": "google",
            "2": "openai",
            "openai": "openai",
            "chatgpt": "openai",
            "codex": "openai",
            "3": "anthropic",
            "claude": "anthropic",
            "anthropic": "anthropic",
            "4": "deepseek",
            "deepseek": "deepseek",
            "5": "groq",
            "groq": "groq",
            "6": "openrouter",
            "openrouter": "openrouter",
        }

        # Reject inline secrets because shell and terminal history may retain them.
        if provider and key:
            print_error("Do not put API keys directly in commands. Use /key <provider> for hidden input.")
            return

        # Case 2: Provider specified without key (/key google)
        if provider:
            target_prov = provider_map.get(provider.lower(), provider.lower())
            if target_prov not in set(provider_map.values()):
                print_error(f"Unknown provider '{provider}'.")
                return
            try:
                secret = getpass.getpass(f"Enter API key for {target_prov} (input hidden): ").strip()
            except (KeyboardInterrupt, EOFError):
                print_warning("\nCancelled.")
                return
            if not secret:
                print_warning("No key entered. Configuration unchanged.")
                return
            self.config.keys[target_prov] = secret
            self.config.key_sources[target_prov] = "config.toml"
            self.config.save()
            await self._rebuild_registry()
            print_success(f"API key for '{target_prov}' saved to ~/.thwip/config.toml")
            return

        # Case 3: Interactive auth table with picker
        table = Table(title="thwip API Credentials & Authentication", box=box.ROUNDED)
        table.add_column("#", style="bold cyan")
        table.add_column("Provider", style="bold white")
        table.add_column("Status", style="white")
        table.add_column("Source", style="dim")
        table.add_column("Env Variable", style="dim")

        providers_order = [
            ("1", "google", "Google Gemini / Antigravity", "GEMINI_API_KEY"),
            ("2", "openai", "OpenAI / ChatGPT / Codex", "OPENAI_API_KEY"),
            ("3", "anthropic", "Anthropic Claude", "ANTHROPIC_API_KEY"),
            ("4", "deepseek", "DeepSeek", "DEEPSEEK_API_KEY"),
            ("5", "groq", "Groq", "GROQ_API_KEY"),
            ("6", "openrouter", "OpenRouter", "OPENROUTER_API_KEY"),
        ]

        for num, prov, label, env_name in providers_order:
            has_key = bool(self.config.keys.get(prov))
            source = self.config.key_sources.get(prov, "none")
            status = "[bold green]Configured[/bold green]" if has_key else "[bold yellow]Missing[/bold yellow]"
            table.add_row(num, label, status, source, env_name)

        console.print(table)
        console.print("[dim]Configure securely with [bold white]/key <provider>[/bold white] or choose a number below.[/dim]")

        try:
            choice = await self._ask_text("Enter choice [1-6] to configure (or press Enter to return):")
            if not choice:
                return
            target_prov = provider_map.get(choice.lower())
            if not target_prov:
                print_warning("Configuration cancelled.")
                return
            secret = getpass.getpass(f"Enter API key for {target_prov} (input hidden): ").strip()
            if not secret:
                print_warning("No key entered. Configuration unchanged.")
                return
            self.config.keys[target_prov] = secret
            self.config.key_sources[target_prov] = "config.toml"
            self.config.save()
            await self._rebuild_registry()
            print_success(f"API key for '{target_prov}' saved to ~/.thwip/config.toml")
        except (KeyboardInterrupt, EOFError):
            print_warning("\nCancelled.")

    def cmd_show_status(self) -> None:
        """Display status."""
        brand = get_brand(self.current_agent.company)
        content = Text()
        content.append(f"Agent:       {self.current_agent.display_name} ({self.current_agent.company})\n", style=brand.label_style)
        content.append(f"Model:       {self.session.current_model}\n", style="bold white")
        content.append(f"Project:     {os.path.abspath(self.session.project_path)}\n", style="white")
        content.append(f"Session:     {self.session.name} ({len(self.session.messages)} messages)\n", style="white")
        content.append(f"Tokens:      {self.session.get_total_tokens():,} used\n", style="dim")
        if getattr(self.current_agent, "native_tools", False):
            content.append("Connection:  Existing CLI sign-in; billing and tool accounting managed by native CLI\n", style="dim")
            content.append(f"Usage:       {describe_limit_windows(getattr(self.current_agent, 'limit_windows', []))}\n", style="dim")
            record = self.session.native_session(self.current_agent.name)
            if record:
                content.append(f"CLI session: {record['id']} (has seen {record['synced']} messages; only new ones are sent)\n", style="dim")
            else:
                content.append("CLI session: none yet; the first message sends the full conversation\n", style="dim")
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
        native = [agent for agent in self.registry.list_agents()
                  if getattr(agent, "native_tools", False) and agent.is_configured()]
        if native:
            windows = Table(title="CLI Account Usage (reported by each CLI)", box=box.ROUNDED)
            windows.add_column("Agent", style="bold white")
            windows.add_column("Usage windows", style="white")
            for agent in native:
                windows.add_row(agent.display_name, describe_limit_windows(getattr(agent, "limit_windows", [])))
            console.print(windows)
            console.print("[dim]Windows update after each native response. Antigravity does not report usage windows.[/dim]")

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
                line = Text("\nYou: ", style="bold cyan")
                line.append(m.content, style="default")
                console.print(line)
            elif m.role == "assistant":
                badge = render_agent_badge(m.agent_name, m.model, m.company)
                console.print("\n", badge)
                console.print(self._render_response(m.content))

    def cmd_show_cost(self) -> None:
        if getattr(self.current_agent, "native_tools", False):
            print_info("Native CLI billing is not included in the API estimates below. Check the CLI account for usage limits.")
        summary = self.usage_tracker.get_summary()
        console.print(f"  [bold]Total Cumulative Spend:[/bold] [green]${summary['total_cost']:.4f}[/green]")
        console.print(f"  [bold]Total Requests:[/bold] {summary['total_requests']}")
        console.print(f"  [bold]Total Tokens Processed:[/bold] {summary['total_tokens']:,}")

    def cmd_project(self, new_path: str = "") -> None:
        if new_path:
            p = Path(new_path).expanduser().resolve()
            if p.is_dir():
                self.session.project_path = str(p)
                self.session.native_sessions.clear()
                self.tool_manager = ToolManager(str(p))
                self.config.project = str(p)
                self.config.save()
                print_success(f"Project path changed to {p}. Native CLI sessions will start fresh here.")
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

    async def process_user_message(self, text: str, _attempted: set[str] | None = None) -> None:
        """Send message to active agent, handle streaming, tool calls, and limits."""
        native = getattr(self.current_agent, "native_tools", False)
        if native:
            self.session.tool_tracking_complete = False
            self.current_agent.project = str(Path(self.session.project_path).resolve())
            if not self.current_agent.is_configured():
                await self.current_agent.refresh_models()
                if not self.current_agent.is_configured():
                    print_error(self.current_agent.discovery_error)
                    return
        if not self.current_agent.is_configured():
            key_name = {
                "google": "GEMINI_API_KEY",
                "openai": "OPENAI_API_KEY",
                "claude": "ANTHROPIC_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
                "groq": "GROQ_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
            }.get(self.current_agent.name, "API_KEY")

            if self.current_agent.name == "ollama":
                guidance = Text(
                    "Ollama server unavailable\n\n"
                    "No API key is required for local Ollama. Start the configured Ollama server "
                    "and make sure the selected model is installed.\n\n"
                    "  - Start a local server: ollama serve\n"
                    "  - Or switch agent: /switch"
                )
            else:
                guidance = Text(f"Direct API setup required for {self.current_agent.display_name}\n\n")
                if getattr(self.current_agent, "auth_method", "none") in ("oauth", "subscription"):
                    guidance.append(
                        "An existing provider CLI sign-in was detected. This Thwip adapter calls "
                        "the provider API directly; it does not run the signed-in CLI or reuse its login.\n\n"
                    )
                guidance.append(
                    f"This adapter requires an API key for {self.session.current_model}.\n\n"
                    f"  - Enter a key securely: /key {self.current_agent.name}\n"
                    f"  - Or configure the {key_name} environment variable\n"
                    "  - Or switch agent: /switch"
                )
                if self.current_agent.name == "openai":
                    guidance.append("\n  - Or use the installed Codex CLI with its own login: /native codex")
                installed = getattr(self.current_agent, "is_installed", None)
                if callable(installed) and installed():
                    guidance.append("\n  - Or reconnect the installed CLI sign-in: /detect")
            console.print(
                Panel(
                    guidance,
                    title="Setup Required",
                    border_style="yellow",
                    box=box.ROUNDED,
                )
            )
            return

        attempted = set() if _attempted is None else set(_attempted)
        attempted.add(self.current_agent.name)
        initial_tool_results = self.session.observed_tool_results
        self.session.add_user_message(text)
        working_messages = self.session.to_portable_messages()

        badge = render_agent_badge(
            self.current_agent.name,
            self.session.current_model,
            self.current_agent.company,
        )
        if getattr(self.config, "display", DisplayConfig()).show_agent_badge:
            console.print("\n", badge)

        # Get tools if agent supports them
        tools = None
        effective_capabilities = self.current_agent.get_capabilities_for_model(self.session.current_model)
        if Capability.FILE_EDIT in effective_capabilities and not native:
            if self.current_agent.name == "claude":
                tools = self.tool_manager.get_anthropic_tools()
            else:
                tools = self.tool_manager.get_openai_tools()

        collected_text = ""
        total_tokens = 0
        limit_hit = False
        native_session_id = ""
        if hasattr(self.current_agent, "get_handoff_models"):
            report = build_handoff_report(self.session, self.current_agent, self.current_agent,
                                          self.session.current_model, tools)
            threshold = getattr(getattr(self.config, "limits", None), "warn_at_percent", 80)
            if report.context_window and report.estimated_input_tokens * 100 >= report.context_window * threshold:
                print_warning(f"Estimated context use reached {threshold}% of the model's catalog limit. "
                              "Use /handoff to inspect; this estimate is approximate.")

        # Tool calls need complete arguments. Run those responses non-streaming,
        # execute them, then send the results back for the model's next turn.
        for _tool_round in range(8):
            round_text = ""
            round_thinking = ""
            tool_requests: list[ToolUseStart] = []
            native_state = {}

            with Live(console=console, refresh_per_second=12) as live:
                try:
                    chat_kwargs = {"messages": working_messages, "model": self.session.current_model,
                                   "system_prompt": self._system_prompt_with_memory(), "tools": tools,
                                   "stream": self.config.stream if tools is None else False}
                    if native:
                        chat_kwargs["resume"] = self.session.native_session(self.current_agent.name)
                    response_stream = self.current_agent.chat(**chat_kwargs)
                    # Close the adapter generator deterministically on interruption so child processes stop.
                    closer = contextlib.aclosing(response_stream) if hasattr(response_stream, "aclose") else contextlib.nullcontext(response_stream)
                    async with closer as response_stream:
                        async for event in response_stream:
                            if isinstance(event, TextDelta):
                                round_text += event.content
                                live.update(self._render_response(collected_text + round_text))
                            elif isinstance(event, ThinkingDelta):
                                round_thinking += event.content
                                live.update(
                                    Panel(
                                        Text(round_thinking, style="dim italic"),
                                        title="Reasoning / Thinking",
                                        border_style="dim magenta",
                                        box=box.MINIMAL,
                                    )
                                )
                            elif isinstance(event, ToolUseStart):
                                tool_requests.append(event)
                            elif isinstance(event, NativePermission):
                                live.stop()
                                console.print(Panel(Text(event.description), title="Native tool permission"))
                                event.approved = await self._ask_yes_no("Allow this operation once? [y/N]:")
                                live.start()
                            elif isinstance(event, NativeActivity):
                                live.stop()
                                print_info(event.description)
                                live.start()
                            elif isinstance(event, AgentDone):
                                native_state = event.native_state
                                native_session_id = str(event.native_session.get("id") or "") or native_session_id
                                used = event.usage.input_tokens + event.usage.output_tokens
                                total_tokens += used
                                self.usage_tracker.record_usage(
                                    agent_name=self.current_agent.name,
                                    model="" if native else self.session.current_model,
                                    input_tokens=event.usage.input_tokens,
                                    output_tokens=event.usage.output_tokens,
                                )
                            elif isinstance(event, LimitHit):
                                limit_hit = True
                                live.stop()
                                self.usage_tracker.record_limit_hit(self.current_agent.name, event.message)
                                if self.session.observed_tool_results != initial_tool_results:
                                    print_warning("Provider limit after tool execution. Not retrying automatically: "
                                                  "review workspace changes before continuing.")
                                else:
                                    await self.handle_limit_failover(event, attempted)
                                break
                except TurnInterrupted:
                    live.stop()
                    raise
                except Exception as exc:
                    live.stop()
                    print_error(f"Agent error: {exc}")
                    if not collected_text and self.session.messages and self.session.messages[-1].role == "user":
                        self.session.messages.pop()
                        print_info("Your message was not answered and was removed from the conversation. "
                                   "Send it again or /switch to another agent.")
                    return

            collected_text += round_text
            if limit_hit or not tool_requests:
                break

            tool_call_messages = []
            tool_result_messages = []
            for request in tool_requests:
                display_args = (
                    {key: value for key, value in request.args.items() if key != "content"}
                    if isinstance(request.args, dict) else "Invalid arguments"
                )
                action = Text(f"\n  Action: {request.tool_name} ", style="bold yellow")
                action.append(str(display_args), style="dim")
                console.print(action)
                approved = True
                read_only = request.tool_name in {"read_file", "list_files", "git_status", "git_diff"}
                if self.config.confirm_tools and not read_only:
                    approved = await self._ask_yes_no("  Allow this action? [y/N]:")
                output = (
                    self.tool_manager.execute_tool(request.tool_name, request.args)
                    if approved
                    else "Denied by user."
                )
                self.session.record_tool_result()
                result_preview = Text("  Result: ", style="green")
                result_preview.append(output[:500], style="dim")
                console.print(result_preview)
                tool_call_messages.append({
                    "id": request.tool_id,
                    "type": "function",
                    "function": {"name": request.tool_name, "arguments": request.args},
                })
                tool_result_messages.append({
                    "role": "tool",
                    "tool_call_id": request.tool_id,
                    "name": request.tool_name,
                    "content": output,
                })

            working_messages.append({
                "role": "assistant", "content": round_text, "tool_calls": tool_call_messages,
                **({"_native_state": native_state} if native_state else {}),
            })
            working_messages.extend(tool_result_messages)
        else:
            print_warning("Stopped after 8 consecutive tool rounds to prevent an infinite loop.")

        if collected_text and not limit_hit:
            self.session.add_assistant_message(
                content=collected_text,
                agent_name=self.current_agent.name,
                model=self.session.current_model,
                company=self.current_agent.company,
                tokens=total_tokens,
            )
            if native and native_session_id:
                # The native session now holds every portable message; next turn sends only what is new.
                self.session.set_native_session(self.current_agent.name, native_session_id, self.session.current_model)
            if getattr(self.config, "auto_save", False):
                if self.session.name == "new-session":
                    self.session.name = f"session-{self.session.id}"
                self.session.save()

    async def handle_limit_failover(self, event: LimitHit, attempted: set[str] | None = None) -> None:
        """Handle rate limit or quota exhaustion with auto-suggested failover."""
        if not self.config.fallback.enabled:
            print_warning("Provider fallback is disabled in configuration.")
            return
        attempted = set(attempted or ()) | {self.current_agent.name}

        # Find ready alternatives
        ready = self.registry.get_ready_agents()
        alternatives = []
        seen: set[str] = set()
        candidates: list[tuple[BaseAgent, str]] = []
        for target in self.config.fallback.chain:
            agent_name, separator, model = target.partition("/")
            agent = self.registry.get_agent(agent_name)
            if not agent or agent.name in attempted or agent not in ready or agent.name in seen:
                continue
            # Only honor a configured chain model that the provider actually lists; otherwise use its default.
            listed = {entry.id for entry in agent.available_models}
            chosen_model = model if separator and model in listed else agent.get_default_model()
            candidates.append((agent, chosen_model))
            seen.add(agent.name)
        candidates.extend(
            (agent, agent.get_default_model())
            for agent in ready
            if agent.name not in attempted and agent.name not in seen
        )

        for a, chosen_model in candidates:
                alternatives.append({
                    "agent": a.display_name,
                    "company": a.company,
                    "model": chosen_model,
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
            print_warning("No untried configured providers remain. Stopped without repeating failed requests.")
            return

        choice = "1" if self.config.limits.auto_switch else await self._ask_text(
            "Switch to alternative agent now? [1 to switch, Enter to cancel]:"
        )
        if choice == "1" or choice.lower() == "y":
            target_alt = alternatives[0]
            for a in ready:
                if a.display_name == target_alt["agent"]:
                    await self.cmd_switch(a.name, target_alt["model"])
                    if self.current_agent is not a or not self.session.messages:
                        return
                    console.print("[bold green]Retrying your last message with new agent...[/bold green]")
                    last_msg = self.session.messages[-1].content
                    self.session.messages.pop()
                    await self.process_user_message(last_msg, attempted)
                    break


MAN_PAGE = Path(__file__).parent / "data" / "thwip.1"


def run_man_command(command: str) -> int:
    """Show or install the bundled manual page. pip cannot install man pages, so thwip does it on request."""
    import subprocess

    if not MAN_PAGE.is_file():
        print("The manual page is missing from this installation.", file=sys.stderr)
        return 1
    if command == "man":
        if shutil.which("man"):
            return subprocess.call(["man", "-l", str(MAN_PAGE)])
        print(MAN_PAGE.read_text(encoding="utf-8"))
        return 0
    target_dir = Path(os.environ.get("THWIP_MAN_DIR") or Path.home() / ".local" / "share" / "man" / "man1")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "thwip.1"
        target.write_text(MAN_PAGE.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        print(f"Could not install the manual page: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {target}")
    print("Run `man thwip`. If it is not found, add this to your shell profile:")
    print(f'  export MANPATH="{target_dir.parent}:$MANPATH"')
    return 0


def main(argv: list[str] | None = None) -> None:
    """Entry point for the thwip CLI command."""
    import argparse

    from thwip import __version__

    parser = argparse.ArgumentParser(
        prog="thwip",
        description="Universal coding agent multiplexer. Run without arguments to open the interactive REPL.",
    )
    parser.add_argument("--version", action="version", version=f"thwip {__version__}")
    parser.add_argument("-p", "--project", metavar="PATH",
                        help="Project directory for this session (defaults to the saved project or the current directory).")
    parser.add_argument("command", nargs="?", choices=["man", "install-man"],
                        help="'man' shows the manual page; 'install-man' installs it so that `man thwip` works.")
    args = parser.parse_args(argv)
    if args.command:
        raise SystemExit(run_man_command(args.command))
    project = None
    if args.project:
        candidate = Path(args.project).expanduser()
        if not candidate.is_dir():
            parser.error(f"project directory '{args.project}' does not exist")
        project = str(candidate.resolve())
    cli = ThwipCLI(project)
    cli.run()


if __name__ == "__main__":
    main()
