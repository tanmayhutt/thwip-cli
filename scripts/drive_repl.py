"""Drive the real thwip REPL through a pseudo-terminal for live verification.

Usage: python scripts/drive_repl.py '<json steps>' <project-dir> [python-executable]
Each step is [wait_regex, text_to_send, timeout_seconds]. Waits only match output produced after the
previous step, so a prompt still on screen cannot trigger a step early. Output is printed with ANSI
codes stripped and spinner frames collapsed. Set THWIP_CONFIG_DIR to keep test runs out of ~/.thwip.
"""

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time


def run(steps, cwd, python=None):
    python = python or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python")
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="120", LINES="45")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(python, ["python", "-m", "thwip.cli"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
    out = b""
    consumed = 0

    def read_until(pattern, timeout):
        nonlocal out, consumed
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                out += chunk
                fresh = out[consumed:].decode("utf-8", "replace")
                match = re.search(pattern, fresh)
                if match:
                    consumed += len(fresh[: match.end()].encode())
                    return True
        return False

    for wait_for, send, timeout in steps:
        ok = read_until(wait_for, timeout)
        print(f"[driver] {wait_for!r} -> {'ok' if ok else 'TIMEOUT'}", file=sys.stderr)
        try:
            os.write(fd, send.encode())
        except OSError:
            break
        time.sleep(0.5)
    read_until(r"$^", 3)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]|\r")
    text = ansi.sub("", out.decode("utf-8", "replace"))
    return re.sub(r"(?:[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] [^\n⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]*)+", "[spinner]", text)


if __name__ == "__main__":
    print(run(json.loads(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
