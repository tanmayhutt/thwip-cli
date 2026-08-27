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

Install in editable mode or via pip:

```bash
git clone https://github.com/thwip-cli/thwip.git
cd thwip
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
| Anthropic | Claude Code (Sonnet, Opus, Haiku) | Chat, File Edit, Code Run, Terminal, Git, Search |
| Google | Antigravity / Gemini CLI (2.5 Pro, 2.5 Flash) | Chat, File Edit, Code Run, Terminal, Browser, Search |
| OpenAI | Codex / ChatGPT (GPT-4.1, o3, o4-mini) | Chat, File Edit, Code Run, Terminal |
| DeepSeek | DeepSeek V3 / R1 Reasoner | Chat, File Edit, Code Run, Reasoning |
| Groq | Llama 3.3 70B, Mixtral | Chat, File Edit, Code Run |
| Ollama | Local Models (Llama 3.3, Qwen Coder, DeepSeek R1) | Chat, File Edit, Code Run (Local, Offline) |
| OpenRouter | Multi-Company Models | Gateway Routing |

---

## Configuration (`~/.thwip/config.toml`)

thwip auto-detects existing API keys from environment variables and existing agent configs (`~/.claude.json`, `~/.gemini/config.json`). Configuration can also be set manually:

```toml
[defaults]
agent = "claude"
model = "claude-sonnet-4"
project = "."
theme = "dark"
stream = true
auto_save = true

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
    "claude/claude-sonnet-4",
    "google/gemini-2.5-pro",
    "openai/gpt-4.1",
    "deepseek/deepseek-chat",
    "ollama/llama3.3"
]
```

---

## License

MIT License
