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
from collections.abc import Sequence

from thwip.agents.base import AgentDone, LimitHit, TextDelta


class CompactionUnavailable(RuntimeError):
    """The worker could not produce a summary (limit hit or empty answer)."""


SUMMARY_SYSTEM_PROMPT = "You write faithful, compact summaries for another assistant that will continue the work."

SUMMARY_REQUEST = (
    "Summarize the conversation below for another assistant that will continue it. Keep every decision, "
    "requirement, file path, command, error, code word, URL, and open task exactly as stated. Use short bullet points "
    "under the headings Context, Decisions, Open tasks. Do not add commentary.\n\n"
)


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

    request = SUMMARY_REQUEST + transcript(messages)
    try:
        data, repairs = await request_json(worker, request, summary_schema(), model=model)
        return render_summary(data), repairs
    except StructuredOutputError:
        return await summarize(worker, messages, model), -1


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
