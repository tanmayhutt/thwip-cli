"""
Utility functions for thwip.

Streaming helpers, markdown rendering, token counting, cost estimation.
"""

from __future__ import annotations

import asyncio
import sys
from typing import AsyncIterator


# ---------------------------------------------------------------------------
# Token & Cost Estimation
# ---------------------------------------------------------------------------

# Approximate cost per 1M tokens (input/output): updated periodically
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-haiku-3.5": {"input": 0.80, "output": 4.0},
    # Google
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    # OpenAI
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "o3": {"input": 2.0, "output": 8.0},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "codex-mini": {"input": 1.50, "output": 6.0},
    # DeepSeek
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    # Groq
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "mixtral-8x7b-32768": {"input": 0.24, "output": 0.24},
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
