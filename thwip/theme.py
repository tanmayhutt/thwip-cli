"""
Theme and terminal styling for thwip.

Each agent/company gets its own brand color, and the UI dynamically
adapts based on which agent is active and what capabilities it has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.markdown import Markdown

from thwip import __version__
from rich.syntax import Syntax
from rich import box

if TYPE_CHECKING:
    from thwip.agents.base import BaseAgent, Capability


# ---------------------------------------------------------------------------
# Company / Provider Brand Colors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrandTheme:
    """Visual identity for an agent's company."""
    primary: str        # Main brand color
    secondary: str      # Accent color
    label_style: str    # Rich style string for labels


BRAND_THEMES: dict[str, BrandTheme] = {
    "anthropic": BrandTheme(
        primary="#D97757",
        secondary="#F5E6D3",
        label_style="bold #D97757",
    ),
    "google": BrandTheme(
        primary="#4285F4",
        secondary="#E8F0FE",
        label_style="bold #4285F4",
    ),
    "openai": BrandTheme(
        primary="#10A37F",
        secondary="#D1FAE5",
        label_style="bold #10A37F",
    ),
    "deepseek": BrandTheme(
        primary="#4D6BFE",
        secondary="#E0E7FF",
        label_style="bold #4D6BFE",
    ),
    "mistral": BrandTheme(
        primary="#FF7000",
        secondary="#FFF3E0",
        label_style="bold #FF7000",
    ),
    "groq": BrandTheme(
        primary="#F55036",
        secondary="#FEE2E2",
        label_style="bold #F55036",
    ),
    "ollama": BrandTheme(
        primary="#E5E7EB",
        secondary="#374151",
        label_style="bold #E5E7EB",
    ),
    "openrouter": BrandTheme(
        primary="#8B5CF6",
        secondary="#EDE9FE",
        label_style="bold #8B5CF6",
    ),
    "default": BrandTheme(
        primary="#A78BFA",
        secondary="#1E1B2E",
        label_style="bold #A78BFA",
    ),
}


def get_brand(company: str) -> BrandTheme:
    """Get brand theme for a company, falling back to default."""
    return BRAND_THEMES.get(company.lower(), BRAND_THEMES["default"])


# ---------------------------------------------------------------------------
# Rich Console Setup
# ---------------------------------------------------------------------------

THWIP_THEME = Theme({
    "info": "dim cyan",
    "warning": "bold yellow",
    "error": "bold red",
    "success": "bold green",
    "muted": "dim",
    "user.label": "bold cyan",
    "agent.label": "bold magenta",
    "capability.yes": "bold green",
    "capability.no": "dim red",
    "capability.warn": "bold yellow",
    "status.ready": "bold green",
    "status.no_key": "bold red",
    "status.limited": "bold yellow",
    "status.local": "bold cyan",
    "status.ide": "dim yellow",
    "badge": "bold dim",
    "prompt": "bold white",
    "header": "bold white on #1E1B2E",
})

console = Console(theme=THWIP_THEME)


# ---------------------------------------------------------------------------
# UI Rendering Helpers
# ---------------------------------------------------------------------------

def render_startup_banner(
    agent_name: str,
    company: str,
    model: str,
    project_path: str,
    session_name: str,
    agents_detected: int,
    agents_ready: int,
) -> Panel:
    """Render the startup banner with ASCII logo and current agent info."""
    brand = get_brand(company)

    lines = Text()
    # Web sign + Wordmark logo in exact website theme
    lines.append("        \\  |  /        ", style="bold #8E8B8B")
    lines.append("████████╗██╗  ██╗██╗    ██╗██╗██████╗ \n", style="bold #F1ECEC")
    lines.append("      -- \\ | / --      ", style="bold #8E8B8B")
    lines.append("╚══██╔══╝██║  ██║██║    ██║██║██╔══██╗\n", style="bold #4285F4")
    lines.append("     - - <(", style="bold #8E8B8B")
    lines.append("●", style="bold #FFFFFF")
    lines.append(")> - -        ", style="bold #8E8B8B")
    lines.append("██║   ███████║██║ █╗ ██║██║██████╔╝\n", style="bold #D97757")
    lines.append("      -- / | \\ --         ", style="bold #8E8B8B")
    lines.append("██║   ██╔══██║██║███╗██║██║██╔═══╝ \n", style="bold #10A37F")
    lines.append("        /  |  \\           ", style="bold #8E8B8B")
    lines.append("██║   ██║  ██║╚███╔███╔╝██║██║     \n", style="bold #F1ECEC")
    lines.append("                          ", style="bold #8E8B8B")
    lines.append("╚═╝   ╚═╝  ╚═╝ ╚══╝╚══╝ ╚═╝╚═╝     \n\n", style="dim")

    lines.append("  Universal Coding Agent Multiplexer", style="bold white")
    lines.append(f"  v{__version__}\n\n", style="dim")
    lines.append("  Agent:       ", style="dim")
    lines.append(f"{agent_name}", style=brand.label_style)
    lines.append(f" ({company})\n", style="dim")
    lines.append("  Model:       ", style="dim")
    lines.append(f"{model}\n", style="bold white")
    lines.append("  Workspace:   ", style="dim")
    lines.append(f"{project_path}\n", style="white")
    lines.append("  Session:     ", style="dim")
    lines.append(f"{session_name}\n", style="white")
    lines.append("  Environment: ", style="dim")
    lines.append(f"{agents_detected}", style="bold white")
    lines.append(" agents installed, ", style="dim")
    lines.append(f"{agents_ready}", style="success")
    lines.append(" ready\n\n", style="dim")
    lines.append("  Quickstart:  Type ", style="dim")
    lines.append("/about", style="bold cyan")
    lines.append(" for guide, ", style="dim")
    lines.append("/switch", style="bold cyan")
    lines.append(" to change agent, ", style="dim")
    lines.append("/agents", style="bold cyan")
    lines.append(" to list tools, ", style="dim")
    lines.append("Ctrl+S", style="bold yellow")
    lines.append(" for switcher.", style="dim")

    return Panel(
        lines,
        border_style=brand.primary,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def render_about_guide(
    agents_detected: int = 0,
    agents_ready: int = 0,
    active_agent: str = "",
    active_model: str = "",
    active_company: str = "",
) -> Panel:
    """Render comprehensive about section and navigation guide."""
    brand = get_brand(active_company)
    content = Text()

    content.append("thwip - Universal Coding Agent Multiplexer\n", style="bold white")
    content.append("Combine, switch, and route across installed AI coding agents seamlessly.\n\n", style="dim")

    content.append("HOW IT WORKS\n", style="bold cyan")
    content.append("  1. Auto-Detection:   Scans environment variables, local CLI agents, and existing credentials\n", style="dim")
    content.append("                       in ~/.claude.json, ~/.gemini/config.json, ~/.config/openai/, or Ollama.\n", style="dim")
    content.append("  2. Unified State:    Maintains conversation history and tool outputs in a portable format.\n", style="dim")
    content.append("  3. Hot-Swapping:     Switch models mid-conversation with zero context loss.\n", style="dim")
    content.append("  4. Universal Tools:  Provides safe file editing, shell commands, execution, and git operations.\n", style="dim")
    content.append("  5. Quota Failover:   Catches HTTP 429 errors and offers immediate one-key fallback switching.\n\n", style="dim")

    content.append("NAVIGATION & COMMANDS\n", style="bold cyan")
    content.append("  /switch <agent> [model]  Hot-swap active model mid-conversation (e.g. /switch google gemini-2.5-pro)\n", style="white")
    content.append("  /agents                  List all discovered agents, status, and capabilities\n", style="white")
    content.append("  /models [agent]          Show available models and context window limits\n", style="white")
    content.append("  /limits                  View live token counts, cumulative costs, and quotas\n", style="white")
    content.append("  /tools                   List all universal file, execution, and git tools\n", style="white")
    content.append("  /session save <name>     Save current conversation to disk\n", style="white")
    content.append("  /session load <name>     Restore and continue a saved session\n", style="white")
    content.append("  /history                 View turn summary across models\n", style="white")
    content.append("  /about or /guide         Display this navigation guide anytime\n", style="white")
    content.append("  /clear                   Reset conversation memory\n", style="white")
    content.append("  /exit or /quit           Exit the REPL\n\n", style="white")

    content.append("KEYBOARD SHORTCUTS\n", style="bold cyan")
    content.append("  Ctrl+S                   Interactive model switcher prompt\n", style="white")
    content.append("  Ctrl+T                   Display current agent status and token counters\n", style="white")
    content.append("  Ctrl+C                   Interrupt current response or tool execution\n", style="white")

    return Panel(
        content,
        title="About & Navigation Guide",
        title_align="left",
        border_style=brand.primary,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def render_agent_badge(agent_name: str, model: str, company: str) -> Text:
    """Render an inline badge for message attribution."""
    brand = get_brand(company)
    badge = Text()
    badge.append("[", style="dim")
    badge.append(f"{model}", style=brand.label_style)
    badge.append("]", style="dim")
    return badge


def render_agents_table(agents: list[dict]) -> Table:
    """
    Render the detected agents table.

    Each agent dict should have:
    name, company, status, status_style, models, capabilities, subscription
    """
    table = Table(
        title="Detected Agents",
        box=box.ROUNDED,
        border_style="dim",
        title_style="bold white",
        show_lines=True,
        padding=(0, 1),
    )

    table.add_column("Agent", style="bold white", min_width=14)
    table.add_column("Company", style="dim", min_width=12)
    table.add_column("Status", min_width=10)
    table.add_column("Models", style="white", min_width=16)
    table.add_column("Capabilities", min_width=24)
    table.add_column("Subscription", min_width=12)

    for agent in agents:
        status_text = Text(agent.get("status", "Unknown"))
        status_text.stylize(agent.get("status_style", "dim"))

        cap_text = Text()
        caps = agent.get("capabilities", [])
        for i, cap in enumerate(caps):
            if i > 0:
                cap_text.append(", ", style="dim")
            cap_text.append(cap, style="capability.yes")

        missing = agent.get("missing_capabilities", [])
        for i, cap in enumerate(missing):
            if caps or i > 0:
                cap_text.append(", ", style="dim")
            cap_text.append(f"-{cap}-", style="capability.no")

        sub = agent.get("subscription", "Unknown")
        sub_style = "success" if sub in ("Active", "Free", "Unlimited") else "warning" if sub == "Unknown" else "error"
        sub_text = Text(sub, style=sub_style)

        table.add_row(
            agent["name"],
            agent.get("company", "-"),
            status_text,
            agent.get("models", "-"),
            cap_text,
            sub_text,
        )

    return table


def render_capability_disclaimer(
    agent_name: str,
    company: str,
    supported: list[str],
    unsupported: list[str],
) -> Panel:
    """Render a capability disclaimer when switching agents."""
    brand = get_brand(company)
    content = Text()

    content.append("Switched to ", style="dim")
    content.append(f"{agent_name}", style=brand.label_style)
    content.append(f" ({company})\n\n", style="dim")

    if supported:
        content.append("  Supported: ", style="success")
        content.append(", ".join(supported), style="white")
        content.append("\n")

    if unsupported:
        content.append("  Not available: ", style="error")
        content.append(", ".join(unsupported), style="dim red")
        content.append("\n\n")
        content.append(
            "  Notice: This agent may not support editing files, running code,\n"
            "  or other actions that your previous agent could do.\n"
            "  Chat capabilities will continue.",
            style="warning",
        )

    border_style = "yellow" if unsupported else brand.primary
    return Panel(
        content,
        title="Agent Switch",
        title_align="left",
        border_style=border_style,
        box=box.ROUNDED,
        padding=(0, 2),
    )


def render_limit_warning(
    agent_name: str,
    company: str,
    error_type: str,
    alternatives: list[dict],
) -> Panel:
    """Render the limit exhaustion warning with alternative suggestions."""
    brand = get_brand(company)
    content = Text()

    content.append(f"Notice: {agent_name}", style=brand.label_style)
    if error_type == "quota_exhausted":
        content.append(" - quota exhausted for this billing period.\n\n", style="warning")
    elif error_type == "rate_limited":
        content.append(" - rate limited. Retry in a moment, or switch.\n\n", style="warning")
    else:
        content.append(f" - {error_type}\n\n", style="warning")

    if alternatives:
        content.append("Ready alternatives:\n", style="dim")
        for i, alt in enumerate(alternatives, 1):
            alt_brand = get_brand(alt.get("company", ""))
            content.append(f"  {i}. ", style="bold white")
            content.append(f"{alt['agent']}", style=alt_brand.label_style)
            content.append(f" -> {alt['model']}", style="white")
            caps = alt.get("capabilities", [])
            if caps:
                content.append(f"  ({', '.join(caps)})", style="dim green")
            content.append("\n")

    return Panel(
        content,
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 2),
    )


def render_dynamic_status_bar(
    agent_name: str,
    company: str,
    model: str,
    capabilities: list[str],
    tokens_used: int = 0,
    cost: float = 0.0,
) -> Text:
    """Render the bottom status bar based on active agent capabilities."""
    brand = get_brand(company)
    bar = Text()

    bar.append(f" {agent_name}", style=brand.label_style)
    bar.append(" | ", style="dim")
    bar.append(f"{model}", style="bold white")
    bar.append(" | ", style="dim")

    cap_labels = [
        ("chat", "chat"),
        ("file_edit", "edit"),
        ("code_run", "run"),
        ("terminal", "term"),
        ("git", "git"),
        ("browser", "web"),
    ]
    for cap_key, label in cap_labels:
        if cap_key in capabilities:
            bar.append(f"[{label}] ", style="success")
        else:
            bar.append(f"[{label}] ", style="dim #444444")

    if tokens_used > 0:
        bar.append("| ", style="dim")
        bar.append(f"{tokens_used:,} tok", style="dim cyan")
    if cost > 0:
        bar.append(" | ", style="dim")
        bar.append(f"${cost:.4f}", style="dim green")

    return bar


def render_user_prompt() -> Text:
    """Render the user input prompt."""
    prompt = Text()
    prompt.append("\n You", style="bold cyan")
    prompt.append(" > ", style="dim cyan")
    return prompt


def render_markdown_response(content: str) -> Markdown:
    """Render agent response as markdown."""
    return Markdown(content, code_theme="monokai")


def render_code_block(code: str, language: str = "python") -> Syntax:
    """Render a syntax-highlighted code block."""
    return Syntax(code, language, theme="monokai", line_numbers=True)


def print_info(message: str) -> None:
    console.print(f"  [info][info] {message}[/info]")


def print_success(message: str) -> None:
    console.print(f"  [success][ok] {message}[/success]")


def print_warning(message: str) -> None:
    console.print(f"  [warning][warn] {message}[/warning]")


def print_error(message: str) -> None:
    console.print(f"  [error][error] {message}[/error]")
