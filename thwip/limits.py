"""
Usage tracking & limit management for thwip.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from thwip.config import get_usage_path
from thwip.utils import estimate_cost


@dataclass
class AgentUsageStats:
    """Cumulative usage statistics for a specific agent."""
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    estimated_cost: float = 0.0
    last_limit_hit_timestamp: float = 0.0
    last_error: str = ""


class UsageTracker:
    """Tracks token usage and costs across sessions and agents."""

    def __init__(self) -> None:
        self.stats: dict[str, AgentUsageStats] = {}
        self.load()

    def record_usage(self, agent_name: str, model: str, input_tokens: int, output_tokens: int) -> None:
        """Record tokens used for an agent call."""
        if agent_name not in self.stats:
            self.stats[agent_name] = AgentUsageStats()

        st = self.stats[agent_name]
        st.input_tokens += input_tokens
        st.output_tokens += output_tokens
        st.request_count += 1
        st.estimated_cost += estimate_cost(model, input_tokens, output_tokens)
        self.save()

    def record_limit_hit(self, agent_name: str, error_msg: str) -> None:
        """Record when an agent hits a rate limit or quota."""
        if agent_name not in self.stats:
            self.stats[agent_name] = AgentUsageStats()
        self.stats[agent_name].last_limit_hit_timestamp = time.time()
        self.stats[agent_name].last_error = error_msg
        self.save()

    def get_summary(self) -> dict[str, Any]:
        """Return summary of total spend and tokens."""
        total_tokens = sum(s.input_tokens + s.output_tokens for s in self.stats.values())
        total_cost = sum(s.estimated_cost for s in self.stats.values())
        total_reqs = sum(s.request_count for s in self.stats.values())
        return {
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "total_requests": total_reqs,
            "by_agent": {k: asdict(v) for k, v in self.stats.items()},
        }

    def save(self) -> None:
        path = get_usage_path()
        data = {k: asdict(v) for k, v in self.stats.items()}
        try:
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def load(self) -> None:
        path = get_usage_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
            for k, v in data.items():
                self.stats[k] = AgentUsageStats(**v)
        except Exception:
            pass
