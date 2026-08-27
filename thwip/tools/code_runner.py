"""
Code execution runners for Python, Node, and scripts.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class CodeRunner:
    """Executes arbitrary code snippets in isolated temporary files."""

    def __init__(self, project_path: str | Path = ".") -> None:
        self.project_path = Path(project_path).resolve()

    def run_python(self, code: str, timeout: int = 20) -> str:
        """Execute a Python snippet."""
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            temp_file = f.name
        try:
            res = subprocess.run(
                ["python3", temp_file],
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = res.stdout.strip()
            err = res.stderr.strip()
            return f"Return code: {res.returncode}\n{out}\n{err}".strip()
        except subprocess.TimeoutExpired:
            return f"Timeout: Python script exceeded {timeout}s."
        except Exception as e:
            return f"Execution error: {e}"
        finally:
            Path(temp_file).unlink(missing_ok=True)

    def run_node(self, code: str, timeout: int = 20) -> str:
        """Execute a Node.js snippet."""
        with tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False) as f:
            f.write(code)
            temp_file = f.name
        try:
            res = subprocess.run(
                ["node", temp_file],
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = res.stdout.strip()
            err = res.stderr.strip()
            return f"Return code: {res.returncode}\n{out}\n{err}".strip()
        except subprocess.TimeoutExpired:
            return f"Timeout: Node script exceeded {timeout}s."
        except Exception as e:
            return f"Execution error: {e}"
        finally:
            Path(temp_file).unlink(missing_ok=True)
