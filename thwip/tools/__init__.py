"""
Tool registry and schema translator for LLMs.
"""

from __future__ import annotations

from typing import Any
from thwip.tools.file_editor import FileEditor
from thwip.tools.terminal import TerminalRunner
from thwip.tools.code_runner import CodeRunner
from thwip.tools.git_ops import GitOps


class ToolManager:
    """Manages tool execution and provides schemas for agent tool calling."""

    def __init__(self, project_path: str = ".") -> None:
        self.project_path = project_path
        self.file_editor = FileEditor(project_path)
        self.terminal = TerminalRunner(project_path)
        self.code_runner = CodeRunner(project_path)
        self.git = GitOps(project_path)

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions formatted for OpenAI / DeepSeek / Groq."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read content of a file in the project.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative path to file"},
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write or overwrite content in a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative path to file"},
                            "content": {"type": "string", "description": "Full file content"},
                        },
                        "required": ["file_path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "Replace exact target string with replacement text in a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative path to file"},
                            "old_str": {"type": "string", "description": "Exact text to find and replace"},
                            "new_str": {"type": "string", "description": "Replacement text"},
                        },
                        "required": ["file_path", "old_str", "new_str"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a shell command in project directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Terminal command to run"},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in the project workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sub_dir": {"type": "string", "description": "Subdirectory to list (default .)"},
                        },
                    },
                },
            },
        ]

    def get_anthropic_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions formatted for Anthropic Claude."""
        return [
            {
                "name": "read_file",
                "description": "Read content of a file in the project.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Relative path to file"},
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "write_file",
                "description": "Write or overwrite content in a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Relative path to file"},
                        "content": {"type": "string", "description": "Full file content"},
                    },
                    "required": ["file_path", "content"],
                },
            },
            {
                "name": "edit_file",
                "description": "Replace exact target string with replacement text in a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Relative path to file"},
                        "old_str": {"type": "string", "description": "Exact text to find and replace"},
                        "new_str": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["file_path", "old_str", "new_str"],
                },
            },
            {
                "name": "run_command",
                "description": "Run a shell command in project directory.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Terminal command to run"},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "list_files",
                "description": "List files in the project workspace.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sub_dir": {"type": "string", "description": "Subdirectory to list (default .)"},
                    },
                },
            },
        ]

    def execute_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Execute a tool by name with arguments and return output."""
        if tool_name == "read_file":
            return self.file_editor.read_file(args.get("file_path", ""))
        elif tool_name == "write_file":
            return self.file_editor.write_file(args.get("file_path", ""), args.get("content", ""))
        elif tool_name == "edit_file":
            return self.file_editor.edit_file(
                args.get("file_path", ""), args.get("old_str", ""), args.get("new_str", "")
            )
        elif tool_name == "run_command":
            return self.terminal.run_command(args.get("command", ""))
        elif tool_name == "list_files":
            return self.file_editor.list_files(args.get("sub_dir", "."))
        elif tool_name == "git_status":
            return self.git.status()
        elif tool_name == "git_diff":
            return self.git.diff(args.get("staged", False))
        elif tool_name == "run_python":
            return self.code_runner.run_python(args.get("code", ""))
        else:
            return f"Error: Unknown tool '{tool_name}'."
