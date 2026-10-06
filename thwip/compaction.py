"""Compaction: shrink a long conversation without losing what matters.

Vocabulary:
- Compaction summarises the older turns into a short, faithful note and keeps the most recent turns
  word for word. The transcript on disk is never deleted; only what is sent to models shrinks.
- The worker is the provider that writes the summary. It must not be the active provider: the thing
  that triggers compaction (a full context or an exhausted quota) is usually the thing that cannot
  serve one more request. Any other ready provider is used; the active one only as a last resort.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence

from thwip.agents.base import AgentDone, LimitHit, TextDelta


class CompactionUnavailable(RuntimeError):
    """The worker could not produce a summary (limit hit or empty answer)."""


SUMMARY_SYSTEM_PROMPT = ("You write faithful, compact summaries for another assistant that will continue the work. "
                         "When unsure whether a detail matters, keep it.")
SUMMARY_REQUEST = (
    "Summarize the conversation below for another assistant that will continue it with no other memory of it.\n"
    "Rules:\n"
    "- Copy every file path, command, error message, code word, identifier, URL, number and version exactly as written; "
    "never paraphrase these.\n"
    "- Keep every decision with its reason, every requirement and constraint the user stated, and every preference "
    "about how the user wants to work.\n"
    "- Keep every open task, question, and anything tried that failed, so it is not tried again.\n"
    "- Drop only greetings, repetition, and reasoning that led nowhere.\n"
    "- Short bullet points under the headings Context, Decisions, Open tasks. No commentary.\n\n"
)

# Things a summary must carry word for word. They are extracted deterministically from the old turns and checked
# against the summary; a summary missing any of them is sent back with the exact list.
_ANCHOR_PATTERNS = [
    re.compile(r"`([^`\n]{2,120})`"),                                      # anything the conversation put in code marks
    re.compile(r"(?<![\w/.])((?:~|\.{1,2})?/?[\w.\-]+(?:/[\w.\-]+)+)"),    # paths with at least one slash
    re.compile(r"\b([\w.\-]+\.(?:py|js|ts|tsx|md|toml|json|yml|yaml|txt|ini|cfg|sh|html|css|rs|go|java|c|h|cpp))\b"),
    re.compile(r"\b(https?://[^\s)\]]+)"),                                  # URLs
    re.compile(r"\b([A-Z][A-Z0-9_-]{3,})\b"),                                # CODE-WORDS and CONSTANTS
    re.compile(r"\bv?(\d+\.\d+(?:\.\d+)+)\b"),                            # versions
]


def extract_anchors(text: str) -> list[str]:
    """Exact strings the summary must contain, in order of first appearance, without duplicates."""
    found: list[str] = []
    for pattern in _ANCHOR_PATTERNS:
        for match in pattern.findall(text):
            item = match.strip()
            if len(item) >= 2 and item not in found:
                found.append(item)
    return found


def missing_anchors(summary: str, anchors: Sequence[str]) -> list[str]:
    lowered = summary.lower()
    return [a for a in anchors if a.lower() not in lowered]


def choose_worker(active, ready: Sequence, chain: Sequence[str] = ()):
    """Pick the provider that writes the summary: never the active one when any alternative is ready."""
    others = [agent for agent in ready if agent is not active and agent.name != getattr(active, "name", None)]
    if not others:
        # Last resort: the active provider itself, if it is at least connected.
        return active if (active in ready or getattr(active, "is_configured", lambda: False)()) else None
    for entry in chain:
        name = entry.partition("/")[0]
        for agent in others:
            if agent.name == name:
                return agent
    return others[0]


def transcript(messages: Sequence[dict]) -> str:
    return "\n\n".join(f"[{m['role'].title()}]\n{m['content']}" for m in messages
                         if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str))


async def summarize_structured(worker, messages: Sequence[dict], model: str | None = None) -> tuple[str, int]:
    """Summary as validated JSON (context, decisions, open_tasks), rendered to Markdown. Returns (text, repairs).

    Falls back to the plain-text summary when the worker cannot produce valid JSON after repairs.
    """
    from thwip.structured import StructuredOutputError, render_summary, request_json, summary_schema
    text = transcript(messages)
    request = SUMMARY_REQUEST + text
    anchors = extract_anchors(text)
    try:
        data, repairs = await request_json(worker, request, summary_schema(), model=model)
    except StructuredOutputError:
        return await summarize(worker, messages, model), -1
    summary = render_summary(data)
    # Coverage check: every exact item from the old turns must appear in the summary. Two rounds, then whatever is
    # still missing is appended verbatim so nothing is silently dropped.
    for _round in range(2):
        missing = missing_anchors(summary, anchors)
        if not missing:
            break
        fix = (request + "\n\nYour previous summary left out these exact items; include each one verbatim where it "
               "belongs:\n- " + "\n- ".join(missing[:40]) + "\n\nPrevious summary:\n" + summary)
        try:
            data, more = await request_json(worker, fix, summary_schema(), model=model)
        except StructuredOutputError:
            break
        repairs += more + 1
        summary = render_summary(data)
    missing = missing_anchors(summary, anchors)
    if missing:
        summary += "\nExact items from earlier turns\n" + "\n".join(f"- {m}" for m in missing[:40])
    return summary, repairs


async def summarize(worker, messages: Sequence[dict], model: str | None = None) -> str:
    """Ask the worker for the summary. Raises RuntimeError on a limit hit or an empty answer."""
    request = SUMMARY_REQUEST + transcript(messages)
    summary = ""
    stream = worker.chat(messages=[{"role": "user", "content": request}], model=model or worker.get_default_model(),
                         system_prompt=SUMMARY_SYSTEM_PROMPT, tools=None, stream=False)
    async with contextlib.aclosing(stream) if hasattr(stream, "aclose") else contextlib.nullcontext(stream) as stream:
        async for event in stream:
            if isinstance(event, TextDelta):
                summary += event.content
            elif isinstance(event, LimitHit):
                raise CompactionUnavailable(f"{worker.display_name} hit a limit while summarizing: {event.message}")
            elif isinstance(event, AgentDone):
                pass
    summary = summary.strip()
    if not summary:
        raise CompactionUnavailable(f"{worker.display_name} returned an empty summary")
    return summary


def split_for_compaction(messages: Sequence[dict], keep_recent: int) -> tuple[list[dict], list[dict]]:
    """(older turns to summarise, recent turns to keep verbatim). Recent turns start on a user message when possible."""
    portable = [m for m in messages if m.get("role") in {"user", "assistant"}]
    if keep_recent <= 0 or len(portable) <= keep_recent:
        return list(portable), []
    cut = len(portable) - keep_recent
    while cut < len(portable) and portable[cut].get("role") != "user":
        cut += 1
    return list(portable[:cut]), list(portable[cut:])


def compacted_messages(summary: str, recent: Sequence[dict]) -> list[dict]:
    """The portable history after compaction: one summary pair followed by the kept turns."""
    return [{"role": "user", "content": "Summary of the conversation so far, compacted by thwip:\n\n" + summary},
            {"role": "assistant", "content": "Understood. I will continue from this summary."}, *recent]
