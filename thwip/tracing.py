"""Tracing: one record per model request, written as a line of JSON you can query later.

A trace answers "what happened on that request": which provider and model, how long it took,
how many tokens, what it cost, how many tool calls, whether it errored. thwip already counted
tokens and cost in totals; traces keep the per-request detail so a slow or expensive turn can be
found after the fact. Records never contain prompt or answer text, only measurements.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from thwip.config import get_config_dir

MAX_TRACE_BYTES = 5 * 1024 * 1024  # rotate the file once it grows past this


@dataclass
class Trace:
    provider: str
    model: str
    kind: str = "chat"            # chat, compaction, memory-update, eval
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    rounds: int = 1
    native: bool = False
    session: str = ""
    error: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def trace_path() -> Path:
    return get_config_dir() / "traces.jsonl"


def record(trace: Trace) -> None:
    """Append one record. Never raises: tracing must not break a turn."""
    try:
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_TRACE_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.to_dict(), separators=(",", ":")) + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def tail(limit: int = 20, provider: str | None = None) -> list[dict]:
    """The most recent records, newest last."""
    path = trace_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (provider is None or row.get("provider") == provider):
                rows.append(row)
    except OSError:
        return []
    return rows[-limit:]


def summarize(rows: list[dict]) -> dict[str, dict]:
    """Per provider: requests, mean latency, tokens, cost, errors."""
    out: dict[str, dict] = {}
    for row in rows:
        item = out.setdefault(row.get("provider", "?"), {"requests": 0, "latency_s": 0.0, "input_tokens": 0,
                                                            "output_tokens": 0, "cost_usd": 0.0, "errors": 0, "tool_calls": 0})
        item["requests"] += 1
        item["latency_s"] += float(row.get("latency_s", 0) or 0)
        item["input_tokens"] += int(row.get("input_tokens", 0) or 0)
        item["output_tokens"] += int(row.get("output_tokens", 0) or 0)
        item["cost_usd"] += float(row.get("cost_usd", 0) or 0)
        item["tool_calls"] += int(row.get("tool_calls", 0) or 0)
        item["errors"] += 1 if row.get("error") else 0
    for item in out.values():
        item["mean_latency_s"] = round(item["latency_s"] / item["requests"], 2) if item["requests"] else 0.0
        item["cost_usd"] = round(item["cost_usd"], 4)
        del item["latency_s"]
    return out
