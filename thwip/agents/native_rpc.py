"""Bounded JSON-RPC transport for installed agent processes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

from thwip.agents.native_common import scrub
from thwip.tools.terminal import terminate_process_tree


class NativeRPC:
    def __init__(self, command: list[str], cwd: str):
        self.command = command
        self.cwd = cwd
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = asyncio.Queue(maxsize=2048)
        self.sequence = 0

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024, start_new_session=os.name == "posix",
        )
        self.reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" not in message and isinstance(message.get("id"), (int, str)) and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
                else:
                    self.events.put_nowait(message)
        except (ValueError, asyncio.QueueFull):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Native CLI connection closed."))
            with contextlib.suppress(asyncio.QueueFull):
                self.events.put_nowait({"method": "_closed"})

    async def send(self, message):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            reply = await asyncio.wait_for(future, timeout)
            if "error" in reply:
                # Bound and scrub server diagnostics so credentials never reach the terminal.
                error = reply["error"] if isinstance(reply["error"], dict) else {}
                detail = scrub(error.get("message", ""), 200)
                suffix = f": {detail}" if detail else "."
                raise RuntimeError(f"Native CLI rejected {method} (code {error.get('code', 'unknown')}){suffix}")
            return reply.get("result", {})
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        if self.process:
            terminate_process_tree(self.process)
            await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
