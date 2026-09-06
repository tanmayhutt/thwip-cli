"""
Terminal execution tools for coding agents.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path


def terminate_process_tree(proc) -> None:
    """Stop only the process group created for this tool invocation."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def run_captured(command, *, cwd: str, timeout: float, shell: bool = False):
    """Capture output and clean up the invocation on timeout or interruption."""
    proc = subprocess.Popen(command, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=os.name == "posix")
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, proc.returncode, out, err)
    except BaseException:
        terminate_process_tree(proc)
        proc.communicate()
        raise


class TerminalRunner:
    """Executes commands and captures output in the project directory."""

    def __init__(self, project_path: str | Path = ".") -> None:
        self.project_path = Path(project_path).resolve()

    def run_command(self, command: str, timeout: int = 30) -> str:
        """Run a shell command synchronously and return combined stdout/stderr."""
        try:
            res = run_captured(
                command,
                shell=True,
                cwd=str(self.project_path),
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
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
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
        except TimeoutError:
            if proc:
                terminate_process_tree(proc)
                await proc.communicate()
            return f"Error: Command timed out after {timeout} seconds."
        except asyncio.CancelledError:
            if proc:
                terminate_process_tree(proc)
                await proc.communicate()
            raise
        except Exception as e:
            return f"Error executing command: {e}"
