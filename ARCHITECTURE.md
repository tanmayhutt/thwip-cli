# thwip — how it works, in plain language

Written 2026-10-03 against version 1.9.2. About 8,700 lines of Python, 21 test files.

## What thwip is

You install one command. Inside it you can talk to seven different AI providers, swap between
them in the middle of a conversation without losing what was said, and let them read and change
files in your project with your approval.

Nothing else does the swapping part. That is the idea the project is built around.

## Status today

| Thing | State |
|---|---|
| Published | Yes, on PyPI as `thwip-cli`, version 1.9.2 |
| Providers | Seven: Anthropic, OpenAI, Google, DeepSeek, Groq, Ollama, OpenRouter |
| Tools the AI can use | Four: file editing, terminal, code running, git |
| Tests | 21 test files, all offline using fake providers |
| Cost tracking | Yes, per provider, saved between runs |
| Evaluation | Started 2026-10-03: `thwip/evals/` with five tasks, an offline fake adapter, and `python -m thwip.evals`. Run live against Claude Code, Antigravity and Codex the same day |
| Retrieval | None. Project notes are pasted in whole, then cut off at 8,000 characters |

## What it does today, in plain language

**Finds what you already have.** On startup it scans your machine for installed AI tools and API
keys, so you do not configure anything by hand. That is `detector.py` and `config.py`.

**Talks to seven providers through one interface.** Each provider has its own adapter file that
knows that provider's quirks. They all follow the same contract, so the rest of the program does
not care which one is active. That is the `agents/` folder.

**Switches providers mid-conversation.** When you switch, it takes everything said so far and
re-sends it to the new provider, because the new provider has never seen any of it.

**Lets the AI touch your files, with limits.** The AI can read files, write files, run commands,
run code and inspect git. It cannot leave your project folder, and anything that changes a file
asks you first. That is the `tools/` folder.

**Keeps a project memory.** It reads a `context.md` file from your project and gives it to the AI
as background, so the AI knows what the project is. That is `memory.py`.

**Tracks what you spend.** Every request records tokens and estimated cost per provider. That is
`limits.py` and `utils.estimate_cost`.

**Warns you before an expensive switch.** It estimates how big the transfer will be and tells you
if you are close to the new provider's limit. That is `handoff.py`.

## What every file does

### The starting points

| File | Lines | What it does |
|---|---|---|
| `__main__.py` | 6 | Lets you run `python -m thwip` |
| `cli.py` | 1,906 | The whole interactive terminal: the prompt loop, every slash command, the display. The biggest file by far |
| `shortcuts.py` | 82 | Tab completion and keyboard shortcuts |
| `theme.py` | 471 | All the colours, banners, tables and markdown rendering. Each provider gets its own brand colour |

### Setup and discovery

| File | Lines | What it does |
|---|---|---|
| `config.py` | 477 | Reads and writes `~/.thwip/config.toml`, finds API keys in environment variables and other tools' config files, runs first-time setup |
| `detector.py` | 283 | Scans your machine for installed AI CLIs, npm packages, pip packages and editor extensions |
| `endpoints.py` | 42 | Lets you point an adapter at a different server, for proxies or self-hosted models |

### The provider adapters

| File | Lines | What it does |
|---|---|---|
| `agents/base.py` | 424 | The contract every adapter must follow. Defines what a capability is, what events can come back while streaming, and what a model's limits are. Everything else inherits from this |
| `agents/__init__.py` | 141 | The registry: finds the adapters, lists which are ready, picks a fallback when one hits a limit |
| `agents/claude_agent.py` | 464 | Anthropic |
| `agents/openai_agent.py` | 459 | OpenAI |
| `agents/google_agent.py` | 437 | Google Gemini |
| `agents/deepseek_agent.py` | 235 | DeepSeek, over an OpenAI-shaped API |
| `agents/openrouter_agent.py` | 223 | OpenRouter, which fronts 100+ models |
| `agents/ollama_agent.py` | 218 | Local models on your own machine. Free, private, no quota |
| `agents/groq_agent.py` | 203 | Groq, the fast one |
| `agents/catalog.py` | 139 | Asks each provider which models it actually offers right now, instead of trusting a hardcoded list |
| `agents/chat_messages.py` | 20 | Converts tool arguments into the format the Chat Completions API expects |

### Driving other people's CLIs

These exist because some tools are signed in through their own app, and thwip should use that
sign-in rather than ask for an API key.

| File | Lines | What it does |
|---|---|---|
| `agents/native_print.py` | 357 | Runs the Claude Code and Antigravity CLIs one turn at a time and reads their streamed output |
| `agents/native_agent.py` | 288 | Talks to the Codex App Server over JSON-RPC |
| `agents/native_common.py` | 129 | Shared helpers: scrubbing output, classifying rate limits, building prompts |
| `agents/native_rpc.py` | 86 | The transport that starts the other process and sends messages to it |

### The tools the AI can use

| File | Lines | What it does |
|---|---|---|
| `tools/__init__.py` | 265 | The registry. Also translates one tool definition into both OpenAI's and Anthropic's formats, because they disagree on the shape |
| `tools/file_editor.py` | 127 | Read, write, edit and list files inside the project folder |
| `tools/terminal.py` | 99 | Run shell commands, with process-tree cleanup so nothing is left running |
| `tools/code_runner.py` | 60 | Run Python and Node snippets |
| `tools/git_ops.py` | 44 | Status, diff and log. Read-only |

### Evaluation

| File | Lines | What it does |
|---|---|---|
| `evals/tasks.py` | ~150 | The task set. Each task says which source file it exercises and carries a deterministic check |
| `evals/runner.py` | ~190 | Runs a task the way the REPL would (tool rounds included), records latency, tokens, cost and tool calls, applies the check, writes JSON |
| `evals/fake_agent.py` | ~70 | A rule-based adapter that follows the event contract, so the harness runs with no network |
| `evals/__main__.py` | ~90 | `python -m thwip.evals`: list, run offline, run live, save a report |

### State, memory and accounting

| File | Lines | What it does |
|---|---|---|
| `session.py` | 291 | Holds the conversation. Converts it to a plain provider-neutral list when switching. Saves and reloads sessions |
| `memory.py` | 412 | Reads and writes the project's `context.md`, and syncs an Obsidian vault of project cards. Only ever overwrites files it created itself |
| `limits.py` | 112 | Records tokens and cost per provider, remembers when you hit a limit, saves it to disk |
| `handoff.py` | 93 | Before a switch, estimates the request size, fingerprints the text, and reports which capabilities you gain or lose |
| `utils.py` | 107 | Cost estimation, token formatting, stream collection, key masking |

## How one question actually flows through the code

1. You type into the prompt in `cli.py`.
2. `session.py` adds your message to the conversation.
3. `memory.py` reads the project's `context.md` and returns up to 8,000 characters of it.
4. `tools/__init__.py` produces the tool definitions in the shape the active provider expects.
5. The active adapter in `agents/` sends: the system prompt, the project memory, the whole
   conversation so far, and the tool definitions.
6. The reply streams back as a series of small events: text, thinking, a tool request, token counts.
7. If the model asks for a tool, the tool runs. If it changes a file, you are asked first.
8. The tool result goes back to the model and it continues.
9. `limits.py` records the tokens and cost.
10. `theme.py` renders it all.

## The context cost when you switch, and how to cut it

### What happens now

Providers keep no memory of your conversation. Everything lives on your machine. So when you
switch, `session.to_portable_messages()` flattens the entire conversation into a plain list and the
new adapter sends all of it again.

Read that function and notice what it does not do: there is no trimming, no summarising, no limit.
Every user and assistant message, from the first one, goes every time.

So a single switch sends:

- the system prompt
- up to 8,000 characters of project memory
- every message in the conversation so far
- the full tool definitions

Your actual question might be twenty words. The rest is overhead, and it repeats on every switch.

`handoff.py` already measures this for you. It reports `estimated_input_tokens` and a
`context_pressure` of "below advisory budget", "near limit" or "likely over budget". The measuring
instrument exists. Nothing acts on it yet.

### Three ways to cut it, in order of payoff

**1. Send the relevant part of the project memory, not the first part.**

`memory.injection()` reads `context.md` and keeps the first 8,000 characters. Your thwip context
file is about 27,700 characters, so roughly two thirds is discarded, and what survives is the top of
the file rather than the part that answers the question.

Fix: split the file into chunks by heading, convert each chunk into an embedding once, store them,
and at question time send only the few chunks closest in meaning to what was asked. Typical result
is a few hundred tokens instead of two thousand, and better answers because there is less noise.

**2. Trim old conversation turns.**

There is already a `/compact` command in `cli.py`, but it is manual. Make it automatic: once the
estimate from `handoff.py` crosses a threshold, summarise the oldest turns into a short paragraph
and keep the recent ones verbatim.

**3. Send tool definitions only when they are needed.**

Full tool schemas go with every request. For a conversation that is not touching files, that is
wasted. Decide per turn whether tools are plausibly needed.

### How to prove it worked

Do item 1 first, and record `estimated_input_tokens` and cost per switch before and after. That
before-and-after number is the single most interesting thing you can put on a resume about this
project, because it is a measurement, not a claim.

## What is missing to call this an AI engineering project

What is already here and counts: multi-provider integration, tool calling with provider-native
continuations, streaming, human approval before any mutation, cost and token accounting, retries
and fallback, offline tests with fake providers.

What is missing:

| Missing | Why it matters |
|---|---|
| Evaluation harness | No way to tell whether a change made the system better or worse. Every job description asks for this |
| Published benchmark | Seven providers wired up and never compared. The comparison is the differentiator nobody else can make cheaply |
| Retrieval | Project memory is truncated rather than searched |
| Structured outputs with validation | Only one adapter touches schemas. No validate-and-repair loop |
| Tracing | Tokens and cost are counted, but there is no per-request record of latency, tool calls and errors you can query |
| Output guardrails | Path containment and approvals protect the filesystem. Nothing checks the model's output itself |

## Build order

1. **Evaluation harness, about two weeks.** 30 to 50 tasks with a defined success condition. Score
   across all seven providers. Record pass rate, latency, cost, tool-call success, malformed output.
   Run it in CI so a regression fails the build. Writing these tasks is also how you will learn this
   codebase properly, because you cannot test the tool layer without reading it.
2. **Published benchmark, a few days.** Put the numbers on the thwip website with the method.
3. **Retrieval in `memory.py`, one to two weeks.** Chunk, embed, store locally, retrieve top matches.
   Measure it with the harness from step 1.
4. **Structured outputs, a few days.** Schemas, validation, repair retry on malformed JSON.
5. **Tracing, a few days.** One record per request: model, latency, tokens, cost, tools, errors.

Skip fine-tuning. Skip rewriting any of this in LangChain; the hand-written adapter layer is the
part that makes the project distinctive.
