"""A deterministic in-process adapter so the harness can be run and tested with no network.

It follows the same event contract as every real adapter (see agents/base.py). Its behaviour is
rule-based: it answers the exact-word task literally, reads note.txt when asked, and politely
declines paths outside the project. `broken=True` produces a deliberately bad adapter so the
harness's failure detection is tested too.
"""

from __future__ import annotations

import json
import re

from thwip.agents.base import (
    AgentDone,
    BaseAgent,
    Capability,
    LimitStatus,
    ModelInfo,
    SubscriptionInfo,
    TextDelta,
    TokenUsage,
    ToolUseStart,
)


class FakeEvalAgent(BaseAgent):
    name = "fake"
    display_name = "Fake (offline)"
    company = "thwip"
    capabilities = {Capability.CHAT, Capability.FILE_READ, Capability.FILE_EDIT}
    available_models = [ModelInfo(id="fake-1", name="Fake 1", is_default=True)]

    def __init__(self, broken: bool = False):
        self.broken = broken

    def is_installed(self):
        return True

    def is_configured(self):
        return True

    def get_install_info(self):
        return {"method": "built-in", "path": "", "version": "1"}

    def get_subscription_info(self):
        return SubscriptionInfo(is_active=True, message="offline fake")

    def check_limits(self):
        return LimitStatus.OK

    async def chat(self, messages, model=None, system_prompt=None, tools=None, stream=True):
        last = messages[-1]
        usage = TokenUsage(input_tokens=sum(len(str(m.get("content", ""))) for m in messages) // 4, output_tokens=4)
        if last.get("role") == "tool":
            output = last.get("content", "")
            match = re.search(r"code word is (\w+)", output, re.IGNORECASE)
            if output.startswith("Error:") and "does not exist" in output:
                text = "That file does not exist in this project, so I cannot say what it fixed."
            elif last.get("name") == "edit_file":
                text = "Done: timeout changed from 30 to 60."
            else:
                text = match.group(1) if match else ("I could not read that file: " + output[:80])
            for piece in (text[:2], text[2:]):
                if piece:
                    yield TextDelta(content=piece)
            yield AgentDone(usage=usage)
            return
        prompt = str(last.get("content", ""))
        if self.broken:
            yield TextDelta(content="pong pong")   # wrong answer, and no AgentDone: violates the contract
            return
        if prompt.startswith("Summarize the conversation below"):
            facts = re.findall(r"CODEWORD-\w+ is \w+", prompt)
            yield TextDelta(content="Context\n- A long routine conversation about build status.\nDecisions\n"
                                    + "".join(f"- {fact}.\n" for fact in facts) + "Open tasks\n- None.")
            yield AgentDone(usage=usage)
            return
        if "Return a single JSON object" in prompt:
            facts = re.findall(r"CODEWORD-\w+ is \w+", prompt)
            yield TextDelta(content=json.dumps({"context": ["conversation summary"], "decisions": facts, "open_tasks": []}))
            yield AgentDone(usage=usage)
            return
        if tools and "change the timeout" in prompt.lower():
            yield ToolUseStart(tool_id="call_edit", tool_name="edit_file",
                               args={"file_path": "config.ini", "old_str": "timeout = 30", "new_str": "timeout = 60"})
            yield AgentDone(usage=usage)
            return
        if tools and "release_notes" in prompt.lower():
            yield ToolUseStart(tool_id="call_rn", tool_name="read_file", args={"file_path": "RELEASE_NOTES.md"})
            yield AgentDone(usage=usage)
            return
        codeword = re.search(r"CODEWORD-(\w+)", prompt) if "?" in prompt else None
        if codeword:
            haystack = (system_prompt or "") + "\n" + "\n".join(str(m.get("content", "")) for m in messages[:-1])
            found = re.search(rf"CODEWORD-{codeword.group(1)}(?::| is) (\S+?)[.\s]", haystack + " ")
            yield TextDelta(content=found.group(1) if found else "I do not have that information.")
        elif "exactly the single word" in prompt:
            word = re.search(r"single word (\w+)", prompt).group(1)
            yield TextDelta(content=word)
        elif tools and "read the file" in prompt.lower():
            path = re.search(r"read the file (\S+)", prompt, re.IGNORECASE).group(1)
            if path.startswith(".."):
                yield TextDelta(content="That path is outside the project, so I will not read it.")
            else:
                yield ToolUseStart(tool_id="call_1", tool_name="read_file", args={"file_path": path})
        else:
            yield TextDelta(content="Hello! ")
            yield TextDelta(content="How can I help?")
        yield AgentDone(usage=usage)
