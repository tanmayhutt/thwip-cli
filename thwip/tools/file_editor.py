"""
File operations tool for coding agents.

Read, write, edit, search files within the project working directory.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class FileEditor:
    """Provides safe file operations rooted in a project directory."""

    def __init__(self, project_path: str | Path = ".") -> None:
        self.project_path = Path(project_path).resolve()

    def _resolve_path(self, file_path: str) -> Path:
        if not file_path or "\x00" in file_path:
            raise ValueError("A valid project-relative path is required.")
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_path / p
        resolved = p.resolve()
        if resolved != self.project_path and self.project_path not in resolved.parents:
            raise ValueError(f"Path '{file_path}' is outside the project workspace.")
        return resolved

    def read_file(self, file_path: str, max_lines: int = 500) -> str:
        """Read content of a file."""
        try:
            target = self._resolve_path(file_path)
        except ValueError as exc:
            return f"Error: {exc}"
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
        try:
            target = self._resolve_path(file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, content)
            return f"Successfully wrote {len(content.splitlines())} lines to '{file_path}'."
        except Exception as e:
            return f"Error writing to file '{file_path}': {e}"

    def edit_file(self, file_path: str, old_str: str, new_str: str) -> str:
        """Replace exact target content with new content in a file."""
        try:
            target = self._resolve_path(file_path)
        except ValueError as exc:
            return f"Error: {exc}"
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
            self._atomic_write(target, new_content)
            return f"Successfully updated '{file_path}'."
        except Exception as e:
            return f"Error editing file '{file_path}': {e}"

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        """Replace a file atomically so interrupted writes cannot corrupt it."""
        temporary_path: Path | None = None
        existing_mode = target.stat().st_mode & 0o777 if target.exists() else None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent,
                prefix=f".{target.name}-", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            if existing_mode is not None:
                temporary_path.chmod(existing_mode)
            temporary_path.replace(target)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

    def list_files(self, sub_dir: str = ".", max_entries: int = 100) -> str:
        """List files and directories within project."""
        try:
            target = self._resolve_path(sub_dir)
        except ValueError as exc:
            return f"Error: {exc}"
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
