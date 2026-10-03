"""Retrieval over project memory: chunking, ranking, budgets, and per-turn excerpts."""

from thwip.memory import ProjectMemory
from thwip.retrieval import chunk_markdown, rank, select, tokenize

DOC = """---
project: demo
area: Tools
---

# demo Context

## Snapshot

- Purpose: demo app

## Current Work

- Now: nothing

## Decisions

- Use SQLite because it needs no server.
- The deploy token rotates on Thursdays; CODEWORD-deploy: THURSDAY.

## Known Issues

- Antigravity resets on hotel wifi.

## Recent Changes

### 2026-09-01

- Added the login page.
""" + "".join(f"\n### 2026-08-{d:02d}\n\n- Filler change number {d} about {'frontend' if d % 2 else 'backend'} work.\n" for d in range(1, 30)) + """
## Notes

""" + "".join(f"- Long note number {i} about the build pipeline and its caching layer, written to exceed one chunk.\n" for i in range(40))


def test_chunking_marks_head_and_splits_long_sections():
    chunks = chunk_markdown(DOC)
    ids = [c.id for c in chunks]
    assert ids[:4] == ["frontmatter", "demo-context", "snapshot", "current-work"]
    assert all(c.is_head for c in chunks[:4]) and not any(c.is_head for c in chunks[4:])
    assert any(c.heading == "2026-08-07" for c in chunks), "dated subsections become their own chunks"
    long_section = [c for c in chunks if c.heading == "Notes"]
    assert len(long_section) > 1 and all(c.text.startswith("## Notes") for c in long_section)
    assert "the" not in tokenize("the deploy token") and "deploy" in tokenize("the deploy token")


def test_ranking_prefers_the_matching_section():
    chunks = chunk_markdown(DOC)
    best = rank(chunks, "when does the deploy token rotate")[0][1]
    assert best.id == "decisions" and "THURSDAY" in best.text
    assert rank(chunks, "hotel wifi resets")[0][1].id == "known-issues"
    assert rank(chunks, "") == []


def test_select_respects_budget_and_exclusions():
    chunks = chunk_markdown(DOC)
    chosen = select(chunks, "deploy token", budget=600)
    assert [c.id for c in chosen][:1] == ["frontmatter"] and sum(len(c.text) for c in chosen) <= 600
    without = select(chunks, "deploy token", budget=5000, exclude={"decisions"}, include_head=False)
    assert "decisions" not in [c.id for c in without] and not any(c.is_head for c in without)


def test_memory_injection_modes(tmp_path):
    (tmp_path / "context.md").write_text(DOC)
    memory = ProjectMemory(str(tmp_path))
    cut = DOC.index("CODEWORD") - 20
    truncated = memory.injection(mode="truncate", budget=cut)
    assert "THURSDAY" not in truncated and "truncated" in truncated, "the old behaviour drops the buried fact"
    retrieved = memory.injection("deploy token rotation", budget=600)
    assert "THURSDAY" in retrieved and "Snapshot" in retrieved and "not shown" in retrieved
    excerpt = memory.excerpt("hotel wifi", exclude={"frontmatter", "snapshot"})
    assert next(c.id for c in excerpt) == "known-issues"
    assert memory.injection("anything", budget=50) == "" or "Project memory" in memory.injection("anything", budget=50)
