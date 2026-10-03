"""Retrieval for project memory: send the parts of context.md that matter for this question.

Vocabulary, kept simple:
- A chunk is one piece of the file, split at headings so each piece is about one topic.
- Scoring ranks chunks by how well their words match the question. This uses BM25, the classic
  keyword-ranking formula behind most search engines. It needs no model, no network and no
  database, and is deterministic, which makes it testable.
- The head of the file (frontmatter, Snapshot, Current Work) is always sent, because it is the
  short "what is this project" part every agent should have regardless of the question.

An embedding backend can be added later behind the same `rank` interface once a local or hosted
embedding model is available; the harness measures whether it beats keywords before we commit.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

HEAD_SECTIONS = {"snapshot", "current work"}
MAX_CHUNK_CHARS = 1400
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-/]{1,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has", "have", "not", "but",
         "its", "into", "when", "then", "than", "also", "only", "each", "every", "per", "via", "all", "any", "can",
         "what", "which", "does", "how", "is", "it", "in", "on", "of", "to", "a", "an", "or", "as", "at", "by", "be"}


@dataclass
class Chunk:
    id: str                 # e.g. "decisions/3"
    heading: str            # the section heading this chunk belongs to
    text: str
    is_head: bool = False   # always included
    tokens: Counter = field(default_factory=Counter, repr=False)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def chunk_markdown(text: str) -> list[Chunk]:
    """Split a context file into chunks: frontmatter, then each heading section, long sections split by bullets."""
    chunks: list[Chunk] = []
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            chunks.append(_make("frontmatter", "frontmatter", body[: end + 4].strip(), is_head=True))
            body = body[end + 4:]
    sections = re.split(r"(?m)^(?=#{1,3} )", body)
    for section in sections:
        section = section.strip()
        if not section:
            continue
        heading_match = re.match(r"^(#{1,3})\s+(.*)$", section, re.MULTILINE)
        heading = heading_match.group(2).strip() if heading_match else "intro"
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"
        is_head = heading.lower() in HEAD_SECTIONS or (heading.endswith("Context") and len(section) < 400)
        if len(section) <= MAX_CHUNK_CHARS:
            chunks.append(_make(slug, heading, section, is_head))
            continue
        # Split a long section at bullet or paragraph boundaries, keeping the heading on every piece.
        pieces, current = [], ""
        for block in re.split(r"\n(?=- |\n|#### |### )", section):
            if current and len(current) + len(block) + 1 > MAX_CHUNK_CHARS:
                pieces.append(current)
                current = block
            else:
                current = f"{current}\n{block}" if current else block
        if current:
            pieces.append(current)
        for index, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece.startswith("#"):
                piece = f"{heading_match.group(0)}\n{piece}" if heading_match else piece
            chunks.append(_make(f"{slug}/{index}", heading, piece, is_head and index == 0))
    return chunks


def _make(chunk_id: str, heading: str, text: str, is_head: bool = False) -> Chunk:
    return Chunk(id=chunk_id, heading=heading, text=text, is_head=is_head, tokens=Counter(tokenize(text)))


def rank(chunks: list[Chunk], query: str, k1: float = 1.5, b: float = 0.75) -> list[tuple[float, Chunk]]:
    """BM25 scores for every chunk against the query, highest first. Zero-score chunks are dropped."""
    terms = tokenize(query)
    if not terms or not chunks:
        return []
    n = len(chunks)
    avg_len = sum(sum(c.tokens.values()) for c in chunks) / n or 1.0
    df = Counter()
    for chunk in chunks:
        for term in set(chunk.tokens):
            df[term] += 1
    scored = []
    for chunk in chunks:
        length = sum(chunk.tokens.values()) or 1
        score = 0.0
        for term in terms:
            tf = chunk.tokens.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / avg_len))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return scored


def select(chunks: list[Chunk], query: str | None, budget: int, exclude: set[str] | None = None,
           include_head: bool = True) -> list[Chunk]:
    """Head chunks first (unless excluded), then the best-matching chunks until the character budget is spent."""
    exclude = exclude or set()
    chosen: list[Chunk] = []
    used = 0
    if include_head:
        for chunk in chunks:
            if chunk.is_head and chunk.id not in exclude and used + len(chunk.text) <= budget:
                chosen.append(chunk)
                used += len(chunk.text)
    candidates = rank(chunks, query) if query else [(0.0, c) for c in chunks if not c.is_head]
    for _score, chunk in candidates:
        if chunk.id in exclude or chunk in chosen:
            continue
        if used + len(chunk.text) > budget:
            continue
        chosen.append(chunk)
        used += len(chunk.text)
    return chosen
