# Verification status

Verified locally on 2026-09-24 for v1.8.0. The Python suite
passes 282 offline tests on Python 3.11, 3.12, and 3.13. Exhaustive behavior across every provider and
configuration has not been established.

## Native CLI connections (2026-09-24)

Live checks were run on macOS through a pseudo-terminal driving the real REPL with
the installed Codex CLI 0.152.1, Claude Code 2.1.281, and Antigravity CLI 1.2.8,
each using its existing sign-in. No API keys were configured.

| Flow | Result |
| --- | --- |
| Startup discovery | All three CLIs connected; live model lists shown (Codex 4 models, Antigravity 14, Claude aliases) |
| Chat turn per provider | Claude Code, Codex, and Antigravity each answered; text streamed into the Live view |
| Context across `/switch` | Codex and Antigravity both recalled the answer given by the previous provider |
| Codex approval request | A write command outside the read-only sandbox produced a permission prompt; denial left the workspace unchanged |
| Ctrl+C during a response | Turn cancelled, child process terminated, REPL continued, unanswered message removed |
| Ctrl+C at a Codex permission prompt | Turn cancelled, no file created, no leftover process, REPL continued |
| Usage-limit failover | With a test-only shim making Codex report "You've hit your usage limit", the real REPL showed the alternatives, switched to Claude Code on `1`, retried the message, and answered; history held one clean pair |
| Project memory and vault (v1.8.0) | Live in a temp project with a temp config and vault: first-run onboarding listed the real Obsidian vault and accepted a typed path; `/memory init` detected the stack and filed card, hub, and dashboard notes; a Codex turn received the memory as instructions; `/memory update` produced a diff written only after `y`; `/status` and the quit-time offer behaved. Offline: frontmatter, template init, injection bounds, Obsidian registry detection, vault sync with related links and hand-written-note protection, update flow, invalid model output rejection, onboarding choices |
| Warm native sessions (v1.7.0) | Live: Claude Code recalled a code word on its second turn through `--resume`; Codex received the full transcript on first switch and answered from it; returning to Claude delivered a catch-up block; a second Codex turn reused the running app-server; `/status` showed each CLI session ID and synced count; `/new` cleared them; no leftover processes. Offline: resume, catch-up, and fallback paths for Codex, Claude Code, and Antigravity; session record validation and persistence |
| Manual page (v1.7.0) | `thwip install-man` then `man -w thwip` and `man thwip` resolve from `~/.local/share/man` |
| Terminal hygiene (v1.6.2) | Cursor-position queries disabled, keyboard echo off during turns, stray input flushed before each prompt; fixes `^[`/`^R` noise and phantom empty prompts seen in a real Ghostty session during a slow Antigravity reply. Permission prompt and chat re-verified live afterwards |
| Ollama adapter (v1.6.1) | End to end against the fake Ollama routes: model list, streamed text with usage, tool call, HTTP 500 and unreachable server now raise clear errors instead of ending the turn silently |
| Dependency audits (v1.6.1) | pip-audit reports no known vulnerabilities; npm audit reports zero |
| Direct API adapters (v1.6.0) | All six (OpenAI, Anthropic, Google, DeepSeek, Groq, OpenRouter) run end to end through their real SDKs over HTTP against `tests/fake_providers.py`: live catalog, streamed text with usage, tool call and result round trip, HTTP 429 to LimitHit. The real REPL was also driven against the fake with direct keys: startup, live `/models`, chat, read_file tool round, provider switches, and 429 failover |
| Full command sweep (v1.5.1) | Every slash command with invalid arguments, native launcher decline, key picker cancel, Ctrl+T, Ctrl+C inside pickers and confirmations, Backspace editing; found and fixed Backspace triggering `/history` via the Ctrl+H binding |
| Parity commands | Live REPL run: `!git log`, `/model` picker, `@file` mention answered by Codex, `/compact` summary, `/export`, `/copy`, `/diff`, `/new`, `/resume`, `/usage` |
| Live catalogs | OpenRouter public list fetched live (459 models with context and pricing); other providers covered by recorded-payload tests because no keys are present here |
| `/session save` and `/session load` | Session with a native provider saved and reloaded in a fresh run |
| `/models`, `/models <provider>`, `/models <tier>` | Live catalogs listed with `CLI account` in place of API pricing |
| `thwip --version`, `--help`, `--project` | Handled without starting the REPL; invalid project exits with code 2 |
| `/limits` and `/status` usage windows | Codex and Claude Code account windows (5h, 7d) displayed with reset times after live turns |
| Leftover processes | None after each run |

The defect that blocked the previous attempt was the Codex sandbox value: the
adapter sent `readOnly` and Codex rejected `thread/start` with an invalid-request
error. The protocol enum is `read-only`. A regression test now checks the request.

Remaining limitations: the Antigravity CLI showed intermittent network resets to
Google's backend during testing, which surface as turn errors; Claude Code's model
aliases are a curated list because the CLI exposes no model listing; native usage
limit failover was observed live only through an injected limit, not a real provider cap; the Gemini CLI path was removed because that CLI is discontinued; Google native is the Antigravity CLI.

| Area | Evidence | Remaining limitation |
| --- | --- | --- |
| Commands and aliases | Offline command smoke tests, invalid input cases | Most smoke tests check exceptions, not all rendered content |
| Sessions | Save/load, separate fresh conversations, malformed metadata, permissions, project rebinding | Concurrent writes to the same explicitly named session are not coordinated |
| Provider switching and handoff | All seven provider catalogs, portable history, bounded failover | Live account quotas and model availability unverified |
| Tool execution | Real temporary-file operations, path containment, process timeout/cancellation, invalid arguments | Shell and code tools retain local user privileges; output capture memory is unbounded |
| Native Codex launcher | Save-before-launch, consent, flags, missing binary, terminal checks, failures | Mocked process replacement; no conversation transfer |
| Native CLI connections | Live REPL runs above; mocked protocol tests for approvals, failures, limits, prompt building, discovery parsing | A real provider usage cap has not been observed; failover proven with an injected limit and with a real HTTP 429 from the fake provider |
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
