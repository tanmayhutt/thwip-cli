"""
Terminal execution tools for coding agents.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path


class TerminalRunner:
    """Executes commands and captures output in the project directory."""

    def __init__(self, project_path: str | Path = ".") -> None:
        self.project_path = Path(project_path).resolve()

    def run_command(self, command: str, timeout: int = 30) -> str:
        """Run a shell command synchronously and return combined stdout/stderr."""
        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = res.stdout.strip()
            err = res.stderr.strip()
            ret = res.returncode
            res_str = f"Exit code: {ret}\n"
            if out:
                res_str += f"STDOUT:\n{out}\n"
            if err:
                res_str += f"STDERR:\n{err}\n"
            return res_str.strip()
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing command: {e}"

    async def run_command_async(self, command: str, timeout: int = 30) -> str:
        """Run a shell command asynchronously."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            ret = proc.returncode
            res_str = f"Exit code: {ret}\n"
            if out:
                res_str += f"STDOUT:\n{out}\n"
            if err:
                res_str += f"STDERR:\n{err}\n"
            return res_str.strip()
        except asyncio.TimeoutError:
            return f"Error: Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing command: {e}"
