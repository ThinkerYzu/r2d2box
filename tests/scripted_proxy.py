#!/usr/bin/env python3
"""A stand-in for agent-proxy that replays a scripted stream.

Run as a real subprocess in place of the real binary, so `proxy.py` is tested
over actual pipes — the buffer limit, the decoding and the shutdown path are
exactly the ones production uses, with no `claude` and no pty anywhere.

    scripted_proxy.py --script tests/fixtures/one-turn.jsonl [proxy args...]

The script is JSON-lines, one directive per line, run in order:

| `op` | Does |
|---|---|
| `emit` | write `message`, stamping `seq` and `outstanding` if absent |
| `await_submit` | block until a `submit` arrives; remember its `ref` |
| `ack` | acknowledge the remembered submit, minting the next turn id |
| `oversize` | write one line of `bytes` junk — for the read-buffer limit |
| `junk` | write `text` as a line, valid JSON or not |
| `exit` | stop, closing stdout |

A `ready` is emitted before the script runs, so a fixture starts at its first
real message. Any argument other than `--script` is ignored, which is what
lets a test pass a full production argv through `build_argv` unchanged.
"""

from __future__ import annotations

import json
import sys

READY_SESSION_ID = "scripted-session-0001"

# Mirrors agent-proxy's envelope: `outstanding` is on every message, absolute
# rather than a delta. Fixtures that do not care about the counts get this.
_IDLE_COUNTS = {"user": 0, "unowned": 0, "background": 0}


def main(argv: list[str]) -> int:
    script_path = _script_path(argv)
    directives = _load(script_path)
    state = _State()
    _emit(state, {"type": "ready", "session_id": READY_SESSION_ID})
    for directive in directives:
        if not _run(state, directive):
            break
    return 0


class _State:
    """Everything the directives share: the seq counter and the pending submit."""

    def __init__(self) -> None:
        self.seq = 0
        self.turns = 0
        self.pending_ref: str | None = None


def _run(state: _State, directive: dict) -> bool:
    """Carry out one directive; return False when the script should stop."""
    op = directive.get("op")
    if op == "emit":
        _emit(state, dict(directive["message"]))
    elif op == "await_submit":
        state.pending_ref = _await_submit()
    elif op == "ack":
        state.turns += 1
        _emit(state, {
            "type": "ack",
            "turn": {"id": f"t-{state.turns}", "kind": "user"},
            "ref": state.pending_ref,
        })
    elif op == "oversize":
        _write_oversize(int(directive["bytes"]))
    elif op == "junk":
        sys.stdout.write(directive["text"] + "\n")
        sys.stdout.flush()
    elif op == "exit":
        return False
    else:
        raise SystemExit(f"scripted_proxy: unknown op {op!r}")
    return True


def _emit(state: _State, message: dict) -> None:
    """Write one message, filling in the envelope fields a fixture left out."""
    state.seq += 1
    message.setdefault("seq", state.seq)
    message.setdefault("outstanding", dict(_IDLE_COUNTS))
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _await_submit() -> str | None:
    """Block until a `submit` command arrives on stdin; return its `ref`.

    Other commands are skipped rather than answered — a fixture that wants a
    `state` reply emits one itself. Returns None if stdin closes first, which
    is how the shutdown test reaches the end of its script.
    """
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if command.get("type") == "submit":
            return command.get("ref")
    return None


def _write_oversize(size: int) -> None:
    """Write a single line of `size` bytes, to overrun the reader's line buffer.

    Written in chunks because the point is a line too big to hold, and the
    fixture's own memory is no more infinite than the reader's. The content is
    `x` rather than JSON: what is being tested is that the reader survives the
    line, not what it says.
    """
    chunk = "x" * (1 << 20)
    written = 0
    while written < size:
        piece = chunk[: min(len(chunk), size - written)]
        sys.stdout.write(piece)
        written += len(piece)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _script_path(argv: list[str]) -> str:
    """Pull `--script <path>` out of the argv; everything else is ignored."""
    for index, arg in enumerate(argv):
        if arg == "--script" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--script="):
            return arg.split("=", 1)[1]
    raise SystemExit("scripted_proxy: --script <path> is required")


def _load(path: str) -> list[dict]:
    """Read the JSON-lines script, skipping blank lines and `#` comments."""
    directives = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                directives.append(json.loads(line))
    return directives


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
