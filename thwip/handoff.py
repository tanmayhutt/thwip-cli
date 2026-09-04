"""Local, non-mutating diagnostics for provider-neutral conversation transfers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from thwip.agents.base import BaseAgent, Capability, ModelInfo
from thwip.session import Session


@dataclass(frozen=True)
class HandoffReport:
    source: str
    target: str
    text_fingerprint: str
    transferred_messages: int
    excluded_messages: int
    excluded_tool_calls: int
    observed_tool_results: int
    tracking_complete: bool
    lost_capabilities: tuple[str, ...]
    gained_capabilities: tuple[str, ...]
    estimated_input_tokens: int
    context_window: int
    output_reserve: int

    @property
    def context_pressure(self) -> str:
        if self.context_window <= 0:
            return "unknown"
        fraction = (self.estimated_input_tokens + self.output_reserve) / self.context_window
        if fraction > 1:
            return "likely over budget"
        if fraction >= 0.8:
            return "near limit"
        return "below advisory budget"


def local_model(agent: BaseAgent, model_id: str) -> ModelInfo | None:
    return next((model for model in agent.get_handoff_models() if model.id == model_id), None)


def local_capabilities(agent: BaseAgent, model_id: str) -> set[Capability]:
    model = local_model(agent, model_id)
    if model and not model.supports_tools:
        return {Capability.CHAT}
    return set(agent.capabilities)


def build_handoff_report(
    session: Session,
    source: BaseAgent,
    target: BaseAgent,
    model_id: str,
    tools: list[dict[str, Any]] | None = None,
) -> HandoffReport:
    """Fingerprint text only; estimate request size without contacting a provider.

    The digest is an equality check, not a signature, confidentiality mechanism,
    or proof that the target provider received or understood the context.
    """
    model = local_model(target, model_id)
    if model is None:
        raise ValueError(f"Unknown model '{model_id}' for {target.name}.")
    portable = session.to_portable_messages()
    text = {"system_prompt": session.system_prompt, "messages": portable}
    canonical = json.dumps(text, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request = json.dumps({**text, "tools": tools or []}, ensure_ascii=False)
    # This heuristic is advisory, especially for code and non-Latin languages.
    estimate = math.ceil(len(request.encode("utf-8")) / 4) + 8 * len(portable)
    old = local_capabilities(source, session.current_model)
    new = local_capabilities(target, model_id)
    return HandoffReport(
        source=f"{source.name}/{session.current_model}",
        target=f"{target.name}/{model_id}",
        text_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        transferred_messages=len(portable),
        excluded_messages=len(session.messages) - len(portable),
        excluded_tool_calls=sum(len(message.tool_calls) for message in session.messages),
        observed_tool_results=session.observed_tool_results,
        tracking_complete=session.tool_tracking_complete,
        lost_capabilities=tuple(sorted(cap.display_name for cap in old - new)),
        gained_capabilities=tuple(sorted(cap.display_name for cap in new - old)),
        estimated_input_tokens=estimate,
        context_window=model.context_window,
        output_reserve=min(4096, model.max_output) if model.max_output > 0 else 4096,
    )
