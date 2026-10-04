"""Structured outputs: ask a model for JSON that matches a schema, validate it, and repair if needed.

Vocabulary:
- A schema is a description of the shape we expect: which keys, which types.
- Validation checks the model's answer against the schema and names the first problem.
- Repair re-asks the model with the exact problem and its previous answer, a bounded number of
  times. Models frequently wrap JSON in prose or code fences, or drop a key; most are fixed in one
  retry. Nothing downstream ever sees unvalidated data.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Sequence
from typing import Any

from thwip.agents.base import AgentDone, LimitHit, TextDelta

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredOutputError(RuntimeError):
    """The model did not produce valid output within the allowed repairs."""


def extract_json(text: str) -> Any:
    """Find the JSON object in a reply that may contain fences or prose around it."""
    candidate = text.strip()
    match = _FENCE.search(candidate)
    if match:
        candidate = match.group(1).strip()
    try:
        return json.loads(candidate)
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            return json.loads(candidate[start:end + 1])
        raise


def validate(data: Any, schema: dict) -> str | None:
    """Return None when `data` matches `schema`, else a plain-language description of the first problem.

    Supported schema keys: type (object, array, string, integer, number, boolean), properties,
    required, items, minItems, enum. Enough for thwip's own outputs without a new dependency.
    """
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(data, dict):
            return f"expected an object, got {type(data).__name__}"
        for key in schema.get("required", []):
            if key not in data:
                return f"missing required key '{key}'"
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                problem = validate(data[key], sub)
                if problem:
                    return f"{key}: {problem}"
        return None
    if kind == "array":
        if not isinstance(data, list):
            return f"expected a list, got {type(data).__name__}"
        if len(data) < schema.get("minItems", 0):
            return f"expected at least {schema['minItems']} items, got {len(data)}"
        for index, item in enumerate(data):
            problem = validate(item, schema.get("items", {}))
            if problem:
                return f"item {index}: {problem}"
        return None
    expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool}.get(kind)
    if expected and (not isinstance(data, expected) or (kind == "integer" and isinstance(data, bool))):
        return f"expected {kind}, got {type(data).__name__}"
    if "enum" in schema and data not in schema["enum"]:
        return f"expected one of {schema['enum']}, got {data!r}"
    return None


async def _ask(agent, prompt: str, model: str | None, system_prompt: str) -> str:
    text = ""
    stream = agent.chat(messages=[{"role": "user", "content": prompt}], model=model or agent.get_default_model(),
                        system_prompt=system_prompt, tools=None, stream=False)
    async with contextlib.aclosing(stream) if hasattr(stream, "aclose") else contextlib.nullcontext(stream) as stream:
        async for event in stream:
            if isinstance(event, TextDelta):
                text += event.content
            elif isinstance(event, LimitHit):
                raise StructuredOutputError(f"{agent.display_name} hit a limit: {event.message}")
            elif isinstance(event, AgentDone):
                pass
    return text


async def request_json(agent, prompt: str, schema: dict, model: str | None = None, retries: int = 2,
                       system_prompt: str = "Reply with JSON only. No prose, no code fences.") -> tuple[Any, int]:
    """Ask for JSON matching `schema`; validate; repair up to `retries` times. Returns (data, repairs_used)."""
    instructions = f"{prompt}\n\nReturn a single JSON object matching this schema exactly:\n{json.dumps(schema)}"
    attempt_prompt = instructions
    last_text = ""
    for attempt in range(retries + 1):
        last_text = await _ask(agent, attempt_prompt, model, system_prompt)
        try:
            data = extract_json(last_text)
            problem = validate(data, schema)
        except ValueError as exc:
            problem = f"not valid JSON ({str(exc)[:80]})"
            data = None
        if problem is None:
            return data, attempt
        attempt_prompt = (f"{instructions}\n\nYour previous answer was rejected: {problem}.\n"
                          f"Previous answer:\n{last_text[:2000]}\n\nReturn only the corrected JSON object.")
    raise StructuredOutputError(f"invalid after {retries} repair attempts: {problem}")


def summary_schema() -> dict:
    return {"type": "object", "required": ["context", "decisions", "open_tasks"],
            "properties": {"context": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                           "decisions": {"type": "array", "items": {"type": "string"}},
                           "open_tasks": {"type": "array", "items": {"type": "string"}}}}


def render_summary(data: dict) -> str:
    """Deterministic Markdown from a validated summary object."""
    def block(title: str, items: Sequence[str]) -> str:
        lines = [f"- {item.strip()}" for item in items if str(item).strip()] or ["- None."]
        return f"{title}\n" + "\n".join(lines)
    return "\n".join([block("Context", data["context"]), block("Decisions", data["decisions"]), block("Open tasks", data["open_tasks"])])
