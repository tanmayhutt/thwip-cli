# thwip

> Universal Coding Agent Multiplexer
> 
> Detect all installed AI coding agents on your device, combine them in a unified terminal interface, and hot-swap models mid-conversation when hitting rate limits without losing context.

---

## Features

- **Auto-Detection**: Discovers installed AI coding agents (Claude Code, Antigravity, Gemini CLI, OpenAI/Codex, Aider, Copilot, Cursor, Windsurf, Cline, Ollama) and configured credentials.
- **Context Portability**: Switch between Anthropic Claude, Google Gemini, OpenAI Codex, or local Ollama mid-project. Conversation history and working files transfer directly.
- **Dynamic UI**: Terminal interface adapts its status bar, capabilities, and theme based on the active provider.
- **Capability Disclaimers**: Highlights when an agent lacks specific capabilities such as file editing or code execution.
- **Rate Limit Failover**: Detects HTTP 429 errors or quota exhaustion and prompts instant switching to ready fallback models.
- **Tool Layer**: Shared file editing, code execution (Python, Node, Shell), and Git integration across connected models.
- **Session Persistence**: Save and resume sessions across projects with `/session save` and `/session load`.
- **Terminal Ergonomics**: Autocompletion (Tab), keyboard shortcuts (Ctrl+S, Ctrl+T, Ctrl+H), and streaming markdown rendering.

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
```

---

## Slash Commands and Shortcuts

| Command | Action |
|:---|:---|
| `/switch [agent] [model]` | Switch active agent or model mid-conversation |
| `/agents` | Show all detected coding agents, company status, and capabilities |
| `/models [agent]` | List available models for current or target agent |
| `/key [provider]` | Enter an API key securely without placing it in prompt history |
| `/status` | Display current session, project, and token stats |
| `/limits` | View token usage, quota, and spend metrics |
| `/detect` | Re-scan system for newly installed coding agents |
| `/session save [name]` | Save current chat session |
| `/session load <name>` | Load a previously saved session |
| `/session list` | List all saved sessions |
| `/session clear` | Clear current conversation memory |
| `/history` | View conversation history with model attribution badges |
| `/cost` | Show estimated session and cumulative cost |
| `/project [path]` | View or change project working directory |
| `Ctrl + S` | Quick switch agent prompt |
| `Ctrl + T` | Show agent status |
| `Ctrl + H` | View history |
| `/quit` | Exit thwip |

---

## Supported Companies and Agents

| Company | Agent | Capabilities |
|:---|:---|:---|
| Anthropic | Claude API (Fable 5, Opus 5, Sonnet 5, Haiku 4.5) | Chat, File Edit, Code Run, Terminal, Git |
| Google | Gemini API (3.1 Pro Preview, 3.7 Flash, 3.5 Flash-Lite) | Chat, File Edit, Code Run, Terminal, Git |
| OpenAI | OpenAI API (GPT-5.6 Sol, Terra, Luna) | Chat, File Edit, Code Run, Terminal, Git |
| DeepSeek | DeepSeek V3 / R1 Reasoner | Chat, File Edit, Code Run, Reasoning |
| Groq | Llama 3.3 70B, Mixtral | Chat, File Edit, Code Run |
| Ollama | Local Models (Llama 3.3, Qwen Coder, DeepSeek R1) | Chat, File Edit, Code Run (Local, Offline) |
| OpenRouter | Multi-Company Models | Gateway Routing |

---

## Configuration (`~/.thwip/config.toml`)

thwip auto-detects existing API keys from environment variables and existing agent configs (`~/.claude.json`, `~/.gemini/config.json`). Configuration can also be set manually:

Installed apps, CLI sign-ins, and API access are separate. thwip can report a detected Claude, Gemini, or Codex CLI login, but the current SDK adapters require a provider API key. A ChatGPT, Claude, or Google subscription does not automatically provide a reusable third-party API key. Ollama needs no key when its local server is running.

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
