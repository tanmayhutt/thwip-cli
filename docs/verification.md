# Verification status

Verified locally on 2026-09-07 for v1.3.0. The Python suite passes 186 tests.
Exhaustive behavior across every provider and
configuration has not been established.

| Area | Evidence | Remaining limitation |
| --- | --- | --- |
| Commands and aliases | Offline command smoke tests, invalid input cases | Most smoke tests check exceptions, not all rendered content |
| Sessions | Save/load, separate fresh conversations, malformed metadata, permissions, project rebinding | Concurrent writes to the same explicitly named session are not coordinated |
| Provider switching and handoff | All seven provider catalogs, portable history, bounded failover | Live account quotas and model availability unverified |
| Tool execution | Real temporary-file operations, path containment, process timeout/cancellation, invalid arguments | Shell and code tools retain local user privileges; output capture memory is unbounded |
| Native Codex launcher | Save-before-launch, consent, flags, missing binary, terminal checks, failures | Mocked process replacement; live native interaction unverified; no conversation transfer |
| Provider responses | Mocked native tool continuations; DeepSeek/Groq/OpenRouter streaming, usage-only chunks, 429/500 errors and serialized tool arguments | Other streaming and error branches still have coverage gaps |
| Display/config/auth | Configuration validation, display settings, credential boundaries, short-key masking | No full terminal/platform matrix |
| Usage | Atomic writes, malformed records, valid totals | Unknown catalog pricing may appear as zero estimated cost |
| Website | Production build and deterministic demo completion/replay test | Real browser, layout, clipboard, keyboard and accessibility checks remain incomplete |
| Dependencies | npm audit: zero advisories; Python installed-dependency audit: none found | Python audit skipped legacy local `thwip 1.0.0` metadata |
| Packaging | Wheel/source build and metadata checks | Publication is separately verified through the release workflow |

The Python suite measured 69% statement coverage with 160 passing tests before
the final credential-masking tests were added. Coverage is diagnostic evidence,
not proof that every feature works. The website test uses a minimal DOM stand-in
and controlled timers, not a browser.

This pass fixed default-session save collisions, malformed session/message
metadata acceptance, tool-argument display crashes, Rich markup interpretation
in action output, and short-key masking leakage.
