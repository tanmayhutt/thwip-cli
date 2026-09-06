"""Translate transient tool arguments into the Chat Completions wire format."""

import json
from typing import Any


def serialize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        converted = {key: value for key, value in message.items() if key != "_native_state"}
        if message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = dict(call["function"])
                if not isinstance(function.get("arguments"), str):
                    function["arguments"] = json.dumps(function.get("arguments", {}))
                calls.append({**call, "function": function})
            converted["tool_calls"] = calls
        result.append(converted)
    return result
