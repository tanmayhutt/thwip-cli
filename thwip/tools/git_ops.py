"""
Git operations tool for coding agents.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitOps:
    """Git version control integration."""

    def __init__(self, project_path: str | Path = ".") -> None:
        self.project_path = Path(project_path).resolve()

    def _run_git(self, args: list[str]) -> str:
        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = res.stdout.strip()
            err = res.stderr.strip()
            if res.returncode != 0:
                return f"Git error (code {res.returncode}): {err or out}"
            return out or "Success."
        except Exception as e:
            return f"Git operation failed: {e}"

    def status(self) -> str:
        return self._run_git(["status", "--short"])

    def diff(self, staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--staged")
        return self._run_git(args)

    def log(self, max_count: int = 5) -> str:
        return self._run_git(["log", f"-n{max_count}", "--oneline"])
