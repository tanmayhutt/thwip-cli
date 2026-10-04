"""Recall over earlier conversation: the part of the transcript that compaction summarised away.

Compaction keeps the transcript on disk and replaces old turns with a summary in what is sent.
Recall closes the loop: when a later question matches an archived turn, that turn is attached
again as an excerpt. Ranking reuses the same BM25 code as project-memory retrieval, treating each
archived turn as one chunk. No model is involved in choosing what to attach.
"""

from __future__ import annotations

from collections.abc import Sequence

from thwip.retrieval import Chunk, rank, tokenize

MAX_TURN_CHARS = 1200


def archive_chunks(archive: Sequence) -> list[Chunk]:
    """One chunk per archived message; long messages are cut to keep excerpts small."""
    chunks: list[Chunk] = []
    for index, message in enumerate(archive):
        role = getattr(message, "role", None) or message.get("role")
        content = getattr(message, "content", None) or message.get("content", "")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        if len(text) > MAX_TURN_CHARS:
            text = text[:MAX_TURN_CHARS].rstrip() + " [cut]"
        label = "User" if role == "user" else "Assistant"
        chunk = Chunk(id=f"turn/{index}", heading=label, text=f"[{label}, earlier]\n{text}")
        from collections import Counter
        chunk.tokens = Counter(tokenize(text))
        chunks.append(chunk)
    return chunks


def excerpt(archive: Sequence, query: str, budget: int = 2000, limit: int = 4) -> list[Chunk]:
    """The archived turns most relevant to `query`, within a character budget."""
    if not archive or not query.strip():
        return []
    chosen: list[Chunk] = []
    used = 0
    for _score, chunk in rank(archive_chunks(archive), query):
        if len(chosen) >= limit or used + len(chunk.text) > budget:
            continue
        chosen.append(chunk)
        used += len(chunk.text)
    chosen.sort(key=lambda c: int(c.id.split("/")[1]))  # chronological order reads better
    return chosen


def render(chunks: Sequence[Chunk]) -> str:
    if not chunks:
        return ""
    return "[Earlier conversation excerpts relevant to this message]\n\n" + "\n\n".join(c.text for c in chunks)
