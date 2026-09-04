"""
Session & conversation state manager for thwip.

Preserves conversational text across provider switches, not native reasoning state.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from thwip.config import get_sessions_dir


def _safe_session_name(value: str) -> str:
    """Return a filename-safe session name that cannot escape the session directory."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    if not cleaned:
        raise ValueError("Session name must contain at least one letter or number.")
    return cleaned[:100]


@dataclass
class Message:
    """A single message in the conversation history."""
    role: str                       # "user" | "assistant" | "system" | "tool"
    content: str
    timestamp: float = field(default_factory=time.time)
    agent_name: str = ""           # Which agent created this (e.g. "claude")
    model: str = ""                # Which model (e.g. "claude-opus-5")
    company: str = ""              # e.g. "Anthropic"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(**data)


@dataclass
class Session:
    """A complete conversation session."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = "new-session"
    project_path: str = "."
    current_agent: str = "claude"
    current_model: str = "claude-opus-5"
    system_prompt: str = (
        "You are an expert AI software engineer. You have tools to inspect files, "
        "write code, run commands, and build software. Be concise, precise, and proactive."
    )
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[Message] = field(default_factory=list)
    observed_tool_results: int = 0
    tool_tracking_complete: bool = True

    def clear_context(self) -> None:
        """Clear text and its associated handoff accounting together."""
        self.messages.clear()
        self.observed_tool_results = 0
        self.tool_tracking_complete = True
        self.updated_at = time.time()

    def record_tool_result(self) -> None:
        """Count transient results without persisting potentially sensitive outputs."""
        self.observed_tool_results += 1
        self.updated_at = time.time()

    def add_user_message(self, content: str) -> Message:
        msg = Message(role="user", content=content)
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    def add_assistant_message(
        self,
        content: str,
        agent_name: str,
        model: str,
        company: str = "",
        tokens: int = 0,
    ) -> Message:
        msg = Message(
            role="assistant",
            content=content,
            agent_name=agent_name,
            model=model,
            company=company,
            tokens=tokens,
        )
        self.messages.append(msg)
        self.updated_at = time.time()
        return msg

    def to_portable_messages(self) -> list[dict[str, Any]]:
        """
        Convert messages to clean LLM-compatible format:
        [{"role": "user"|"assistant", "content": "..."}]
        """
        portable = []
        for m in self.messages:
            if m.role in ("user", "assistant"):
                portable.append({"role": m.role, "content": m.content})
        return portable

    def switch_agent(self, agent_name: str, model: str) -> None:
        """Switch current agent and model while preserving stored text history."""
        self.current_agent = agent_name
        self.current_model = model
        self.updated_at = time.time()

    def get_total_tokens(self) -> int:
        return sum(m.tokens for m in self.messages)

    def save(self, custom_name: str | None = None) -> Path:
        """Save session to ~/.thwip/sessions/<id>.json."""
        if custom_name:
            self.name = _safe_session_name(custom_name)
        else:
            self.name = _safe_session_name(self.name)
        sessions_dir = get_sessions_dir()
        file_path = sessions_dir / f"{self.name}.json"
        data = {
            "id": self.id,
            "name": self.name,
            "project_path": self.project_path,
            "current_agent": self.current_agent,
            "current_model": self.current_model,
            "system_prompt": self.system_prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
            "observed_tool_results": self.observed_tool_results,
            "tool_tracking_complete": self.tool_tracking_complete,
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=sessions_dir,
                prefix=f".{self.name}-", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary.name, 0o600)
                json.dump(data, temporary, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(file_path)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
        return file_path

    @classmethod
    def load(cls, name_or_id: str) -> Session | None:
        """Load session by name or id."""
        sessions_dir = get_sessions_dir()
        try:
            safe_name = _safe_session_name(name_or_id)
        except ValueError:
            return None
        path = sessions_dir / f"{safe_name}.json"
        if not path.is_file():
            # Try searching all sessions
            for f in sessions_dir.glob("*.json"):
                try:
                    d = json.loads(f.read_text())
                    if d.get("id") == name_or_id or d.get("name") == name_or_id:
                        path = f
                        break
                except Exception:
                    continue
        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text())
            session = cls(
                id=data.get("id", ""),
                name=data.get("name", "saved-session"),
                project_path=data.get("project_path", "."),
                current_agent=data.get("current_agent", "claude"),
                current_model=data.get("current_model", "claude-opus-5"),
                system_prompt=data.get("system_prompt", ""),
                created_at=data.get("created_at", time.time()),
                updated_at=data.get("updated_at", time.time()),
                messages=[Message.from_dict(m) for m in data.get("messages", [])],
                observed_tool_results=data.get("observed_tool_results", 0),
                tool_tracking_complete=data.get("tool_tracking_complete", False),
            )
            return session
        except Exception:
            return None

    @classmethod
    def list_saved_sessions(cls) -> list[dict[str, Any]]:
        """List all saved sessions with summary stats."""
        sessions_dir = get_sessions_dir()
        result = []
        for f in sessions_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                result.append({
                    "id": data.get("id"),
                    "name": data.get("name", f.stem),
                    "agent": data.get("current_agent"),
                    "model": data.get("current_model"),
                    "messages_count": len(data.get("messages", [])),
                    "updated_at": datetime.fromtimestamp(
                        data.get("updated_at", 0), tz=UTC
                    ).astimezone().strftime("%Y-%m-%d %H:%M"),
                })
            except Exception:
                continue
        return sorted(result, key=lambda x: x["updated_at"], reverse=True)
