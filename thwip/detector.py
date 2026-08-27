"""
System-wide coding agent & LLM detector.

Scans the local machine for:
1. CLI binaries (claude, agy, gemini, codex, aider, copilot, ollama)
2. npm global packages
3. Python / pip packages
4. VS Code & Cursor extensions (Cline, Continue, Copilot, etc.)
5. macOS Applications (/Applications/Cursor.app, /Applications/Windsurf.app)
6. Environment variables & agent credential files
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thwip.agents.base import Capability


@dataclass
class DetectedTool:
    """An AI coding tool or agent discovered on the system."""
    name: str
    company: str
    category: str               # "CLI Agent", "IDE App", "Editor Extension", "Local Engine"
    install_path: str
    version: str | None = None
    is_configured: bool = False
    config_source: str = ""
    capabilities: list[str] = field(default_factory=list)
    subscription_status: str = "Unknown"


# Comprehensive signature database of AI coding tools
KNOWN_CLI_TOOLS: list[dict[str, Any]] = [
    {
        "name": "Claude Code",
        "company": "Anthropic",
        "binaries": ["claude"],
        "category": "CLI Agent",
        "capabilities": ["Chat", "File Edit", "Code Run", "Terminal", "Git", "Search"],
        "key_env": "ANTHROPIC_API_KEY",
        "config_files": ["~/.claude.json", "~/.claude/config.json"],
    },
    {
        "name": "Antigravity / Gemini CLI",
        "company": "Google",
        "binaries": ["agy", "gemini", "antigravity"],
        "category": "CLI Agent",
        "capabilities": ["Chat", "File Edit", "Code Run", "Terminal", "Browser", "Search"],
        "key_env": "GEMINI_API_KEY",
        "config_files": ["~/.config/gemini/config.json", "~/.gemini/config.json"],
    },
    {
        "name": "Codex / OpenAI CLI",
        "company": "OpenAI",
        "binaries": ["codex", "openai"],
        "category": "CLI Agent",
        "capabilities": ["Chat", "File Edit", "Code Run", "Terminal"],
        "key_env": "OPENAI_API_KEY",
        "config_files": ["~/.config/openai/config.json"],
    },
    {
        "name": "Aider",
        "company": "Independent",
        "binaries": ["aider"],
        "category": "CLI Agent",
        "capabilities": ["Chat", "File Edit", "Git"],
        "key_env": "OPENAI_API_KEY",
        "config_files": ["~/.aider.conf.yml"],
    },
    {
        "name": "GitHub Copilot CLI",
        "company": "GitHub / Microsoft",
        "binaries": ["github-copilot-cli", "copilot"],
        "category": "CLI Agent",
        "capabilities": ["Chat", "Terminal Suggestions"],
        "key_env": "GITHUB_TOKEN",
        "config_files": ["~/.config/github-copilot/hosts.json"],
    },
    {
        "name": "Ollama",
        "company": "Ollama",
        "binaries": ["ollama"],
        "category": "Local Engine",
        "capabilities": ["Chat", "File Edit", "Code Run"],
        "key_env": "",
        "config_files": [],
    },
]

KNOWN_APPS: list[dict[str, Any]] = [
    {
        "name": "Cursor",
        "company": "Cursor Inc",
        "mac_path": "/Applications/Cursor.app",
        "category": "IDE App",
        "capabilities": ["Chat", "File Edit", "Terminal", "Codebase Indexing"],
    },
    {
        "name": "Windsurf",
        "company": "Codeium",
        "mac_path": "/Applications/Windsurf.app",
        "category": "IDE App",
        "capabilities": ["Chat", "File Edit", "Terminal", "Cascade Flows"],
    },
]

KNOWN_EXTENSIONS: list[dict[str, Any]] = [
    {
        "id": "saoudrizwan.claude-dev",
        "name": "Cline (Claude Dev)",
        "company": "Independent",
        "capabilities": ["Chat", "File Edit", "Terminal", "Browser"],
    },
    {
        "id": "continue.continue",
        "name": "Continue.dev",
        "company": "Continue",
        "capabilities": ["Chat", "File Edit", "Autocomplete"],
    },
    {
        "id": "github.copilot",
        "name": "GitHub Copilot",
        "company": "Microsoft",
        "capabilities": ["Chat", "Autocomplete"],
    },
]


class SystemDetector:
    """Scans and discovers all AI coding agents and installed environments."""

    def __init__(self) -> None:
        self.detected_tools: list[DetectedTool] = []

    def scan_all(self) -> list[DetectedTool]:
        """Perform a full system discovery scan."""
        tools: list[DetectedTool] = []

        # 1. Scan CLI Tools
        for spec in KNOWN_CLI_TOOLS:
            binary_found = None
            for b in spec["binaries"]:
                p = shutil.which(b)
                if p:
                    binary_found = p
                    break

            is_configured = False
            config_source = ""

            # Check environment variables
            if spec.get("key_env") and os.environ.get(spec["key_env"]):
                is_configured = True
                config_source = f"env:{spec['key_env']}"

            # Check config files
            if not is_configured:
                for c_file in spec.get("config_files", []):
                    cp = Path(c_file).expanduser()
                    if cp.is_file():
                        is_configured = True
                        config_source = str(cp)
                        break

            # Ollama is configured if binary is present
            if spec["name"] == "Ollama" and binary_found:
                is_configured = True
                config_source = "localhost:11434"

            if binary_found or is_configured:
                # Try getting version
                ver = None
                if binary_found:
                    try:
                        res = subprocess.run(
                            [binary_found, "--version"],
                            capture_output=True,
                            text=True,
                            timeout=3,
                        )
                        if res.returncode == 0:
                            ver = res.stdout.strip().split("\n")[0]
                    except Exception:
                        pass

                sub_status = "Active" if is_configured else "Needs Key"
                if spec["name"] == "Ollama":
                    sub_status = "Unlimited"

                tools.append(
                    DetectedTool(
                        name=spec["name"],
                        company=spec["company"],
                        category=spec["category"],
                        install_path=binary_found or "(API only)",
                        version=ver,
                        is_configured=is_configured,
                        config_source=config_source,
                        capabilities=spec["capabilities"],
                        subscription_status=sub_status,
                    )
                )

        # 2. Scan macOS /Applications
        for app in KNOWN_APPS:
            p = Path(app["mac_path"])
            if p.exists():
                tools.append(
                    DetectedTool(
                        name=app["name"],
                        company=app["company"],
                        category=app["category"],
                        install_path=str(p),
                        version="Installed",
                        is_configured=True,
                        config_source="macOS App",
                        capabilities=app["capabilities"],
                        subscription_status="Active",
                    )
                )

        # 3. Scan VS Code extensions
        try:
            res = subprocess.run(
                ["code", "--list-extensions"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if res.returncode == 0:
                installed_exts = res.stdout.lower().splitlines()
                for ext in KNOWN_EXTENSIONS:
                    if ext["id"].lower() in installed_exts:
                        tools.append(
                            DetectedTool(
                                name=f"{ext['name']} (VS Code)",
                                company=ext["company"],
                                category="Editor Extension",
                                install_path="VS Code Extension",
                                version="Installed",
                                is_configured=True,
                                config_source="VS Code Config",
                                capabilities=ext["capabilities"],
                                subscription_status="Active",
                            )
                        )
        except Exception:
            pass

        self.detected_tools = tools
        return tools
