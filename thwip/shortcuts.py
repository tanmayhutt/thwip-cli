"""
Keyboard shortcut bindings & autocompletion for thwip terminal interface.
"""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings


SLASH_COMMANDS = [
    ("/switch", "Switch agent and model mid-conversation"),
    ("/agents", "Show all detected coding agents & status"),
    ("/models", "List available models for current agent"),
    ("/status", "Display current session and agent info"),
    ("/limits", "View token usage, quota, and spend metrics"),
    ("/detect", "Re-scan system for newly installed coding agents"),
    ("/session save", "Save current chat session"),
    ("/session load", "Resume a previously saved session"),
    ("/session list", "List all saved sessions"),
    ("/session clear", "Clear current conversation context"),
    ("/history", "Show conversation with model attribution badges"),
    ("/project", "View or change project working directory"),
    ("/cost", "Show estimated cost breakdown"),
    ("/help", "Show all available commands and keyboard shortcuts"),
    ("/quit", "Exit thwip"),
]


class ThwipCompleter(Completer):
    """Provides smart auto-completion for slash commands and agent names."""

    def __init__(self, agent_names: list[str] | None = None) -> None:
        self.agent_names = agent_names or ["claude", "google", "openai", "deepseek", "groq", "ollama", "openrouter"]

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            # Command autocompletion
            for cmd, desc in SLASH_COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)

            # /switch <agent> autocompletion
            if text.startswith("/switch "):
                parts = text.split(" ")
                prefix = parts[1] if len(parts) > 1 else ""
                for name in self.agent_names:
                    if name.startswith(prefix):
                        yield Completion(f"/switch {name}", start_position=-len(text), display_meta="Agent")


def create_keybindings(on_switch=None, on_status=None, on_history=None) -> KeyBindings:
    """Create keybindings for prompt_toolkit."""
    kb = KeyBindings()

    @kb.add("c-s")
    def _switch(event):
        """Ctrl+S: Quick switch prompt."""
        event.app.current_buffer.text = "/switch "
        event.app.current_buffer.cursor_position = len(event.app.current_buffer.text)

    @kb.add("c-t")
    def _status(event):
        """Ctrl+T: Status view."""
        event.app.current_buffer.text = "/status"
        event.app.current_buffer.validate_and_handle()

    @kb.add("c-h")
    def _history(event):
        """Ctrl+H: Show history."""
        event.app.current_buffer.text = "/history"
        event.app.current_buffer.validate_and_handle()

    return kb
