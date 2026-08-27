"""
File operations tool for coding agents.

Read, write, edit, search files within the project working directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class FileEditor:
    """Provides safe file operations rooted in a project directory."""

    def __init__(self, project_path: str | Path = ".") -> None:
        self.project_path = Path(project_path).resolve()

    def _resolve_path(self, file_path: str) -> Path:
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_path / p
        return p.resolve()

    def read_file(self, file_path: str, max_lines: int = 500) -> str:
        """Read content of a file."""
        target = self._resolve_path(file_path)
        if not target.is_file():
            return f"Error: File '{file_path}' does not exist."
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > max_lines:
                preview = "\n".join(lines[:max_lines])
                return f"{preview}\n\n... [Truncated: showing first {max_lines} of {len(lines)} lines]"
            return "\n".join(lines)
        except Exception as e:
            return f"Error reading file '{file_path}': {e}"

    def write_file(self, file_path: str, content: str) -> str:
        """Write/overwrite content to a file, creating parent directories if needed."""
        target = self._resolve_path(file_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content.splitlines())} lines to '{file_path}'."
        except Exception as e:
            return f"Error writing to file '{file_path}': {e}"

    def edit_file(self, file_path: str, old_str: str, new_str: str) -> str:
        """Replace exact target content with new content in a file."""
        target = self._resolve_path(file_path)
        if not target.is_file():
            return f"Error: File '{file_path}' does not exist."
        try:
            content = target.read_text(encoding="utf-8")
            if old_str not in content:
                return f"Error: Target text to replace not found in '{file_path}'."
            count = content.count(old_str)
            if count > 1:
                return f"Error: Target text found {count} times. Please specify a more unique target block."
            new_content = content.replace(old_str, new_str, 1)
            target.write_text(new_content, encoding="utf-8")
            return f"Successfully updated '{file_path}'."
        except Exception as e:
            return f"Error editing file '{file_path}': {e}"

    def list_files(self, sub_dir: str = ".", max_entries: int = 100) -> str:
        """List files and directories within project."""
        target = self._resolve_path(sub_dir)
        if not target.is_dir():
            return f"Error: Directory '{sub_dir}' does not exist."
        entries = []
        try:
            for root, dirs, files in os.walk(target):
                # Skip hidden folders like .git, node_modules, __pycache__
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules" and d != "__pycache__"]
                for f in files:
                    if f.startswith("."):
                        continue
                    full_p = Path(root) / f
                    rel_p = full_p.relative_to(self.project_path)
                    entries.append(str(rel_p))
                    if len(entries) >= max_entries:
                        break
                if len(entries) >= max_entries:
                    break
            if not entries:
                return "Directory is empty."
            return "\n".join(sorted(entries))
        except Exception as e:
            return f"Error listing directory: {e}"
