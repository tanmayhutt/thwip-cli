# thwip

> Universal Coding Agent Multiplexer
> 
> Discover supported local AI tools, connect configured model providers in one terminal interface, and switch providers while preserving completed text conversation history.

---

## Features

- **Auto-Detection**: Discovers installed AI coding agents (Claude Code, Antigravity, Gemini CLI, OpenAI/Codex, Aider, Copilot, Cursor, Windsurf, Cline, Ollama) and configured credentials.
- **Existing Sign-ins**: Chats through installed Codex, Claude Code, and Antigravity CLIs using their own logins and live model lists, with no API key required.
- **Context Portability**: Switch providers with stored conversational text. Working files remain in the selected local project; they are not automatically uploaded.
- **Handoff Preview**: Inspect text continuity, omitted state, capability changes, approximate context pressure, and a text fingerprint before switching. Runs locally without model calls.
- **Dynamic UI**: Terminal interface adapts its status bar, capabilities, and theme based on the active provider.
- **Capability Disclaimers**: Highlights when an agent lacks specific capabilities such as file editing or code execution.
- **Rate Limit Failover**: Detects HTTP 429 errors or quota exhaustion and prompts instant switching to ready fallback models.
- **Tool Layer**: Shared file editing, code execution (Python, Node, Shell), and Git integration across connected models.
- **Session Persistence**: Save and resume sessions across projects with `/session save` and `/session load`.
- **Familiar Flow**: The same everyday commands as Codex, Claude Code, and Antigravity (`/model`, `/new`, `/resume`, `/compact`, `/diff`, `/copy`, `!cmd`, `@file`), plus autocompletion, Ctrl+S / Ctrl+T shortcuts, and streaming Markdown.
- **Live Model Lists**: Model catalogs come from the connected CLI or the provider's list-models endpoint, never from a hardcoded list.

---

## Quickstart

### 1. Installation

Install the published package:

```bash
pip install --upgrade thwip-cli
```

Or install the repository in editable mode for development:

```bash
git clone https://github.com/tanmayhutt/thwip-cli.git
cd thwip-cli
pip install -e .
```

### 2. Launching thwip

Start the interactive terminal in your current project directory:

```bash
thwip
thwip --project ~/code/my-app   # open a specific project
thwip --version
```

---

## Slash Commands and Shortcuts

| Command | Action |
|:---|:---|
| `/switch [agent] [model]` | Switch active agent or model mid-conversation |
| `/handoff [agent] [model]` | Preview a target locally without switching or sending data |
| `/native codex` | Save and leave Thwip for the installed Codex CLI itself (launcher, no context transfer) |
| `/agents` | Show all detected coding agents, company status, and capabilities |
| `/models [agent]` | List available models for current or target agent |
| `/key [provider]` | Enter an API key securely without placing it in prompt history |
| `/status` | Display current session, project, and token stats |
| `/limits`, `/usage` | View token usage, CLI account usage windows, and spend metrics |
| `/detect` | Re-scan installed agents and reconnect CLI sign-ins |
| `/model [id]` | Pick a model for the current agent from a numbered list or by ID |
| `/new` | Start a fresh conversation; the current one is saved first |
| `/resume [name]` | Resume a saved session from a numbered list |
| `/compact` | Summarize the conversation with the current model to free context; the summary is plain text and travels across providers |
| `/diff [staged]` | Show the project's git diff |
| `/copy` | Copy the last response to the clipboard |
| `/export [path]` | Write the conversation to Markdown with per-message model attribution |
| `!<command>` | Run a shell command in the project without involving a model |
| `@path` in a message | Attach a project file's content to the message (files outside the project are ignored) |
| `/session save [name]` | Save current chat session |
| `/session load <name>` | Load a previously saved session |
| `/session list` | List all saved sessions |
| `/session clear` | Clear current conversation memory |
| `/history` | View conversation history with model attribution badges |
| `/cost` | Show estimated session and cumulative cost |
| `/project [path]` | View or change project working directory |
| `Ctrl + S` | Quick switch agent prompt |
| `Ctrl + T` | Show agent status |
| `/quit` | Exit thwip |

Short aliases are available for frequent commands: `/a`, `/m`, `/s`, `/sw`, `/k`, `/g`, and `/t`.

### Existing CLI sign-ins (no API key needed)

If Codex, Claude Code, or the Antigravity CLI is installed and signed in, Thwip
connects to it at startup and uses that sign-in for chat. No API key is copied or
required, and Thwip never reads the CLI's stored credentials. A configured direct
API key for the same provider always takes precedence over the native connection.

| Provider | Installed CLI | Transport | Model list |
|:---|:---|:---|:---|
| OpenAI | `codex` | Codex App Server (JSON-RPC over stdio) | Live from `model/list` |
| Anthropic | `claude` | Claude Code print mode (`stream-json`) | Aliases `fable`, `opus`, `sonnet`, `haiku`; explicit IDs pass through |
| Google | `agy` (Antigravity CLI) or `gemini` | Antigravity print mode (`stream-json`) or Gemini ACP | Live from `agy models` or the ACP session |

`/models` refreshes the catalog supplied by each CLI. An explicit model ID that is
not in the catalog is passed to the CLI for validation instead of being rejected by
a bundled list. A model available in a desktop app may still be absent from the
installed CLI's catalog or account access.

Native connections are read-only by default. Codex starts with a read-only sandbox
and asks before operations outside it; approval requests appear in Thwip with the
command or file list and default to denial. Claude Code and the Antigravity CLI run
in their non-interactive print modes, where tools that would need an approval are
declined by the CLI itself. Each turn sends the portable text conversation to a
fresh native session, so native reasoning and tool state do not carry between turns.
Ctrl+C interrupts the current turn and stops the child process; the unanswered
message is removed so it can be re-sent or handed to another provider.

Native billing and usage limits are managed by each CLI account. Thwip records the
token counts the CLIs report but does not estimate cost for them. Codex and Claude
Code also report their account usage windows (5 hour and 7 day) after each response;
`/limits` and `/status` show the used percentage and reset time. When a CLI reports
an exhausted usage limit, the standard failover prompt offers the other connected
providers.

`/native codex` remains available as a launcher: it saves the Thwip session and
replaces Thwip with the Codex CLI itself in the selected project. Conversation
history is not transferred by the launcher; use `/session load` after restarting
Thwip to resume.

## Live model lists

Model lists are not hardcoded. Each connected source supplies its own list:

- Installed CLIs report their models (Codex `model/list`, `agy models`, Claude Code aliases).
- Direct providers with a key are queried through their list-models endpoint (OpenAI,
  Anthropic, Google, DeepSeek, Groq, OpenRouter) at startup, on `/models`, and right
  after `/key`. Non-chat models (embeddings, speech, image, moderation) are filtered
  out. Context sizes and prices are taken from the provider when it publishes them.
- A small bundled list per provider remains only as an offline fallback and is labelled
  "bundled fallback" in `/models` until a key or connection is available.

## Usage-limit failover

When the active provider reports an exhausted limit (Codex "You've hit your usage
limit", Claude Code "usage limit reached" or a rejected rate-limit event, Google
`RESOURCE_EXHAUSTED`, or HTTP 429 from a direct API), Thwip stops, shows the ready
alternatives with their default models, and offers to switch. Accepting switches the
provider, keeps the text conversation, and re-sends the unanswered message. Set
`[limits] auto_switch = true` to skip the question. The `[fallback] chain` order is
honored; a chain model the provider does not list falls back to that provider's default.

## Auditable handoffs

```text
/handoff
/handoff google
/handoff openai gpt-5.6-terra
```

`/handoff` previews the current target; specifying a provider uses its default model
unless you supply a model ID. Targets can be inspected without credentials or an
installed provider. `/switch` shows the same report before changing the active agent.

The report includes:

- Exact counts of transferred user/assistant text messages and excluded stored records.
- A count of observed transient tool results that are not in portable history. New
  sessions track from creation; older saved sessions explicitly report partial coverage.
- Capability gains and losses using the local model catalog.
- Approximate request size including tool schemas, with up to 4,096 tokens reserved
  for an answer in the advisory calculation. This does not change generation settings.
- SHA-256 of canonical system-prompt and conversational-text JSON. It stays the same
  across targets when that text is unchanged. Tool schemas and attribution metadata
  are intentionally outside this text fingerprint.

The preview makes no model requests, saves no transcript exports, executes no tools,
and does not trim or summarize history. Token sizing uses UTF-8 bytes divided by four
plus per-message overhead, not a provider tokenizer. Catalog limits can be stale;
an apparently fitting request can still fail. Warnings are advisory, not switch gates.
Hidden reasoning and provider-native state do not transfer. The digest proves neither
delivery nor semantic understanding, and is not a signature or privacy guarantee.

See [research and prior art](docs/handoff-research.md) for the differentiation rationale.

## Supported Companies and Agents

| Company | Agent | Capabilities |
|:---|:---|:---|
| Anthropic | Claude Code sign-in, or Claude API (Fable 5, Opus 5, Sonnet 5, Haiku 4.5) | Chat, File Edit, Code Run, Terminal, Git |
| Google | Antigravity or Gemini CLI sign-in, or Gemini API (3.1 Pro Preview, 3.7 Flash, 3.5 Flash-Lite) | Chat, File Edit, Code Run, Terminal, Git |
| OpenAI | Codex CLI sign-in, or OpenAI API (GPT-5.6 Sol, Terra, Luna) | Chat, File Edit, Code Run, Terminal, Git |
| DeepSeek | DeepSeek V3 / R1 Reasoner | Chat, File Edit, Code Run, Reasoning |
| Groq | GPT-OSS 120B (default); Llama 3.3 for eligible enterprise accounts only | Chat, File Edit, Code Run |
| Ollama | Local Models (Llama 3.3, Qwen Coder, DeepSeek R1) | Chat, File Edit, Code Run (Local, Offline) |
| OpenRouter | Multi-Company Models | Gateway Routing |

---

## Configuration (`~/.thwip/config.toml`)

thwip auto-detects existing API keys from environment variables and existing agent configs (`~/.claude.json`, `~/.gemini/config.json`). Configuration can also be set manually:

Installed CLI sign-ins and API access are separate paths. A signed-in Codex, Claude Code, or Antigravity CLI is used directly through its own protocol (see above). The direct SDK adapters for Anthropic, Google, OpenAI, DeepSeek, Groq, and OpenRouter require a provider API key. Ollama needs no key when its local server is running.

```toml
[defaults]
agent = "claude"
model = "claude-opus-5"
project = "."
theme = "dark"
stream = true
auto_save = true
confirm_tools = true

[keys]
anthropic = "sk-ant-..."
google = "AIza..."
openai = "sk-..."
deepseek = "sk-..."
groq = "gsk_..."
openrouter = "sk-or-..."

[ollama]
host = "http://localhost:11434"

[fallback]
enabled = true
chain = [
    "claude/claude-opus-5",
    "google/gemini-3.7-flash",
    "openai/gpt-5.6-terra",
    "deepseek/deepseek-v4-flash",
    "ollama/llama3.3"
]
```

Prefer `/key <provider>` over editing the file directly. Mutating workspace tools ask for confirmation by default, file access is restricted to the selected project, and Thwip stores its config and saved sessions with user-only permissions.

---

## License

MIT License
