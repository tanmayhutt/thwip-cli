"""A local stand-in for the direct provider APIs, used to exercise the real SDK code paths.

It speaks enough of the OpenAI (chat completions, responses, models), Anthropic
(messages, models) and Gemini (generateContent, models) HTTP surfaces for the
adapters to stream text, request a tool call, report usage, and hit a 429.
Any model whose ID contains "limited" answers 429; any user text containing
"read the file" produces a read_file tool call when tools were offered.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

REPLY = "fake reply from local provider"
CHAT_MODELS = ["gpt-fake-chat", "gpt-fake-limited"]
GEMINI_MODELS = ["gemini-fake-chat", "gemini-fake-limited"]


def _wants_tool(body: dict) -> bool:
    text = json.dumps(body)
    return "read the file" in text.lower() and ("tools" in body) and not _has_tool_result(body)


def _has_tool_result(body: dict) -> bool:
    text = json.dumps(body)
    return '"tool"' in text or "function_call_output" in text or "tool_result" in text or "functionResponse" in text


def _is_limited(body: dict, path: str) -> bool:
    return "limited" in str(body.get("model", "")) or "limited" in path


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-provider/1"

    def log_message(self, *args):  # silence
        pass

    def _json(self, code: int, payload: dict, headers: dict | None = None):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, events: list[tuple[str | None, dict]]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for name, payload in events:
            if name:
                self.wfile.write(f"event: {name}\n".encode())
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        self.server.requests.append(("GET", path))
        if path.endswith("/models") and "v1beta" in path:
            return self._json(200, {"models": [
                {"name": f"models/{m}", "displayName": m, "supportedGenerationMethods": ["generateContent"],
                 "inputTokenLimit": 32000, "outputTokenLimit": 8000} for m in GEMINI_MODELS]})
        if path.endswith("/models"):
            return self._json(200, {"data": [{"id": m, "display_name": m, "context_window": 32000} for m in CHAT_MODELS]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        query = urlparse(self.path).query
        body = self._body()
        self.server.requests.append(("POST", path))
        if _is_limited(body, path):
            return self._json(429, {"error": {"message": "Rate limit reached for model; quota exhausted", "type": "rate_limit_error"}},
                              {"retry-after": "0"})
        if path.endswith("/chat/completions"):
            return self._chat_completions(body)
        if path.endswith("/responses"):
            return self._responses(body)
        if path.endswith("/v1/messages"):
            return self._anthropic(body)
        if ":streamGenerateContent" in path or ":generateContent" in path:
            return self._gemini(body, stream=":streamGenerateContent" in path or "alt=sse" in query)
        return self._json(404, {"error": f"no route for {path}"})

    # --- OpenAI compatible ---
    def _chat_completions(self, body):
        usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
        if body.get("stream"):
            chunks = []
            for i, piece in enumerate(["fake ", "reply ", "from local provider"]):
                chunks.append((None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                                      "choices": [{"index": 0, "delta": {"content": piece, **({"role": "assistant"} if i == 0 else {})}, "finish_reason": None}]}))
            chunks.append((None, {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}))
            self._sse(chunks)
            self.wfile.write(b"data: [DONE]\n\n")
            return None
        if _wants_tool(body):
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                       "function": {"name": "read_file", "arguments": json.dumps({"file_path": "note.txt"})}}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": REPLY}
            finish = "stop"
        return self._json(200, {"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                                "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})

    def _responses(self, body):
        if _wants_tool(body):
            output = [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file",
                       "arguments": json.dumps({"file_path": "note.txt"}), "status": "completed"}]
        else:
            output = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": REPLY, "annotations": []}]}]
        return self._json(200, {"id": "resp_1", "object": "response", "created_at": 1, "model": body["model"], "status": "completed",
                                "output": output, "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
                                "parallel_tool_calls": True, "tool_choice": "auto", "tools": []})

    # --- Anthropic ---
    def _anthropic(self, body):
        usage = {"input_tokens": 11, "output_tokens": 5}
        if body.get("stream"):
            self._sse([
                ("message_start", {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "model": body["model"],
                                                                         "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fake reply "}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "from local provider"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ])
            return None
        if _wants_tool(body):
            content = [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"file_path": "note.txt"}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": REPLY}]
            stop = "end_turn"
        return self._json(200, {"id": "m1", "type": "message", "role": "assistant", "model": body["model"], "content": content,
                                "stop_reason": stop, "stop_sequence": None, "usage": usage})

    # --- Gemini ---
    def _gemini(self, body, stream):
        usage = {"promptTokenCount": 11, "candidatesTokenCount": 5, "totalTokenCount": 16}
        if _wants_tool(body):
            parts = [{"functionCall": {"name": "read_file", "args": {"file_path": "note.txt"}}}]
        else:
            parts = [{"text": REPLY}]
        candidate = {"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}
        payload = {"candidates": [candidate], "usageMetadata": usage, "modelVersion": "fake"}
        if stream:
            self._sse([(None, payload)])
            return None
        return self._json(200, payload)


class FakeProviderServer:
    """Threaded local HTTP server. `url` is the base URL to point adapters at."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.server.requests


if __name__ == "__main__":  # manual run: python tests/fake_providers.py
    import time
    with FakeProviderServer() as fake:
        print(fake.url, flush=True)
        while True:
            time.sleep(3600)
