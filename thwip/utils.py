"""
Utility functions for thwip.

Streaming helpers, markdown rendering, token counting, cost estimation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

# ---------------------------------------------------------------------------
# Token & Cost Estimation
# ---------------------------------------------------------------------------

# Approximate cost per 1M tokens (input/output): updated periodically
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    # Google
    "gemini-3.1-pro-preview": {"input": 2.0, "output": 12.0},
    "gemini-3.7-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    # OpenAI
    "gpt-5.6-sol": {"input": 4.0, "output": 20.0},
    "gpt-5.6-terra": {"input": 2.0, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
    # DeepSeek
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    # Groq
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
    # Mistral
    "mistral-large-latest": {"input": 2.0, "output": 6.0},
    "codestral-latest": {"input": 0.30, "output": 0.90},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given model and token counts."""
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return 0.0
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


def format_cost(cost: float) -> str:
    """Format cost as a readable string."""
    if cost < 0.01:
        return f"${cost:.6f}"
    elif cost < 1.0:
        return f"${cost:.4f}"
    else:
        return f"${cost:.2f}"


def format_tokens(count: int) -> str:
    """Format token count with commas."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


# ---------------------------------------------------------------------------
# Async Helpers
# ---------------------------------------------------------------------------

async def collect_stream(stream: AsyncIterator[str]) -> str:
    """Collect all chunks from an async stream into a single string."""
    parts: list[str] = []
    async for chunk in stream:
        parts.append(chunk)
    return "".join(parts)


# ---------------------------------------------------------------------------
# String Helpers
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int = 80) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def mask_key(key: str) -> str:
    """Mask an API key for display: sk-ant-...xyz123"""
    if not key or len(key) < 10:
        return "****"
    return f"{key[:7]}...{key[-6:]}"


def slugify(text: str) -> str:
    """Convert text to a URL/filename-safe slug."""
    import re
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")
