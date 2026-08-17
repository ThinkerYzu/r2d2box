"""Where a conversation lives once its turn is over.

Transcript storage belongs to the library rather than to each host, so the
`TranscriptStore` interface here is the whole contract: four methods, all
async, so a host can back them with a database or an HTTP service instead of a
file. Two implementations ship — `MemoryTranscriptStore` for tests and for a
host that wants nothing on disk, and `FileTranscriptStore` for everyone else.

A store holds *completed* turns. The turn in flight lives in the session until
its `turn_end` arrives, which is why `Session.snapshot` reads both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# The longest readable part of a slugged file or directory name. The digest is
# appended past this, so the cap costs legibility and never uniqueness.
_SLUG_TEXT_LIMIT = 60

# Characters a slug may keep verbatim. Everything else collapses to a dash,
# which is what keeps `../` and a bare `.` out of a path.
_SLUG_KEEP = re.compile(r"[^A-Za-z0-9._-]+")

# Marks a transcript file as one of ours and records the keys its name was
# slugged from, so `list_sessions` can give back the session id the caller
# used rather than the mangled form on disk.
_META_MARKER = "r2d2box-transcript"

# The longest a `SessionInfo.preview` may be. Long enough to tell two questions
# apart in a list, short enough that listing a topic stays a small response
# whatever was pasted into the box.
_PREVIEW_LIMIT = 120

# Collapses every run of whitespace in a preview to one space, so a pasted
# stack trace becomes one line rather than the first line of one.
_PREVIEW_SPACE = re.compile(r"\s+")


@dataclass
class Turn:
    """One exchange in a conversation: a prompt, and everything the agent said back.

    `id` and `kind` come from agent-proxy's `turn` object. `user` is the text
    the person typed, not the prompt that was sent — a host's `build_prompt`
    may have prepended half a document to it, and the transcript should show
    what was asked. It is None for an unowned turn, which nobody submitted.

    `by_host` marks a turn whose `user` text is the host application's own
    words rather than a person's question — the turn a conversation opens with
    is the one that happens. It is what keeps such a turn out of
    `SessionInfo.preview`, where it would label every session identically.

    `events` are the forwarded messages with r2d2box's envelope already on
    them, so replaying a transcript to a client and
    watching one live deliver the same objects.
    """

    id: str
    kind: str = "user"
    user: str | None = None
    by_host: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The JSON form written to a store and sent to a client.

        `events` is copied rather than shared. A turn still running keeps
        appending to its own list, and a caller holding this dict — a snapshot
        already handed to a client, a turn already given to a store — must not
        find events in it that arrived after it asked.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "user": self.user,
            "by_host": self.by_host,
            "events": list(self.events),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        """Rebuild a turn from `to_dict`'s output, tolerating fields it lacks.

        Unknown keys are dropped and missing ones take their defaults, so a
        transcript written by an older r2d2box still loads. One default is
        visible rather than merely safe: a turn written before `by_host`
        existed reads as a person's, so a conversation stored back then previews
        with whatever opened it.
        """
        return cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", "user")),
            user=data.get("user"),
            by_host=bool(data.get("by_host")),
            events=list(data.get("events") or []),
            started_at=float(data.get("started_at") or 0.0),
            ended_at=data.get("ended_at"),
            outcome=data.get("outcome"),
        )


@dataclass(frozen=True)
class SessionInfo:
    """A stored session as the host's session picker needs to list it.

    `turns` counts exchanges, not the messages inside them: one turn is one
    line of a stored transcript, and it is what a person reading a list of
    conversations counts. `preview` is the first thing a person asked in this
    one, as `preview_of` trims it, or None for a conversation nobody has spoken
    in yet.

    Both carry defaults so a host's own `TranscriptStore` still constructs a
    `SessionInfo` from the two fields that were here first; such a store lists
    every session as empty and unlabelled until it fills them.
    """

    session: str
    last_active: float
    turns: int = 0
    preview: str | None = None


def preview_of(turns: Iterable[Turn]) -> str | None:
    """A one-line label for a conversation: the first thing a person asked in it.

    None until someone has asked something. Turns the host submitted itself are
    skipped — a conversation that opens with a briefing would otherwise be
    labelled with the same briefing as every other one — and so are turns with
    no `user` text, which nobody submitted.

    Whitespace is collapsed and the result cut to `_PREVIEW_LIMIT`, so a pasted
    stack trace becomes a readable line rather than the first line of one and a
    listing stays small whatever was typed.
    """
    for turn in turns:
        if turn.by_host or not turn.user:
            continue
        text = _PREVIEW_SPACE.sub(" ", turn.user).strip()
        if text:
            return text[:_PREVIEW_LIMIT]
    return None


class TranscriptStore(ABC):
    """Storage for finished turns, keyed by topic and session.

    Every method is async so a host can substitute a store that talks to a
    database or a service. Both keys are opaque strings chosen by the host: an
    implementation that maps them onto anything hierarchical — paths, URLs,
    table names — has to make them safe itself.

    Reading a session that was never written is not an error; it is an empty
    transcript.
    """

    @abstractmethod
    async def append_turn(self, topic: str, session: str, turn: Turn) -> None:
        """Add one completed turn to the end of a session's transcript."""

    @abstractmethod
    async def read_turns(self, topic: str, session: str) -> list[Turn]:
        """Every stored turn for a session, oldest first."""

    @abstractmethod
    async def list_sessions(self, topic: str) -> list[SessionInfo]:
        """Every session with a stored transcript under `topic`, most recent first.

        This is what a session picker is drawn from, so an implementation is
        expected to fill `turns` and `preview` as well — cheaply, since it is
        called for a whole topic at once. Leaving them at their defaults costs
        the picker its labels and nothing else.
        """

    @abstractmethod
    async def clear(self, topic: str, session: str) -> None:
        """Discard a session's transcript. Silent if there was none."""


class MemoryTranscriptStore(TranscriptStore):
    """A store that keeps everything in a dict and loses it on restart.

    The default when a host names no store, and what the session tests run
    against. Turns are held by reference, so a caller that mutates a turn it
    appended changes what a later `read_turns` gives back — fine for the
    session, which appends a turn it is finished with.
    """

    def __init__(self) -> None:
        self._turns: dict[tuple[str, str], list[Turn]] = {}
        self._last_active: dict[tuple[str, str], float] = {}

    async def append_turn(self, topic: str, session: str, turn: Turn) -> None:
        key = (topic, session)
        self._turns.setdefault(key, []).append(turn)
        self._last_active[key] = turn.ended_at or time.time()

    async def read_turns(self, topic: str, session: str) -> list[Turn]:
        return list(self._turns.get((topic, session), ()))

    async def list_sessions(self, topic: str) -> list[SessionInfo]:
        found = []
        for (stored_topic, session), turns in self._turns.items():
            if stored_topic == topic:
                found.append(SessionInfo(
                    session=session,
                    last_active=self._last_active.get((stored_topic, session), 0.0),
                    turns=len(turns),
                    preview=preview_of(turns),
                ))
        return sorted(found, key=lambda info: info.last_active, reverse=True)

    async def clear(self, topic: str, session: str) -> None:
        self._turns.pop((topic, session), None)
        self._last_active.pop((topic, session), None)


class FileTranscriptStore(TranscriptStore):
    """One JSON-lines file per session, one directory per topic, under a root.

    The file's first line is a metadata record naming the topic and session it
    came from; every line after it is one turn. Nothing is held in memory, so
    a process restart loses nothing and two processes sharing a root append to
    the same files without coordinating.

    Both keys are slugged into the path, so a topic may be any string a host
    likes — `Bug 1992198: crash on resize` included — without deciding where
    the file lands.

    The file I/O runs on the event loop rather than in a thread. Appending one
    turn to a local file is a few hundred microseconds, and a host that needs
    better than that has somewhere other than a local disk in mind, which is
    what the `TranscriptStore` interface is for. `list_sessions` is the one
    method that reads whole files — a turn count is a line count — so a topic
    holding many long conversations pays for its listing in I/O.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    async def append_turn(self, topic: str, session: str, turn: Turn) -> None:
        path = self._path(topic, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            if handle.tell() == 0:
                handle.write(json.dumps(_meta_record(topic, session)) + "\n")
            handle.write(json.dumps(turn.to_dict()) + "\n")

    async def read_turns(self, topic: str, session: str) -> list[Turn]:
        return _read_turns(self._path(topic, session))

    async def list_sessions(self, topic: str) -> list[SessionInfo]:
        directory = self._root / _slug(topic)
        if not directory.is_dir():
            return []
        found = []
        for path in directory.glob("*.jsonl"):
            summary = _summarize_transcript(path)
            if summary is not None:
                name, turns, preview = summary
                found.append(SessionInfo(
                    session=name,
                    last_active=path.stat().st_mtime,
                    turns=turns,
                    preview=preview,
                ))
        return sorted(found, key=lambda info: info.last_active, reverse=True)

    async def clear(self, topic: str, session: str) -> None:
        self._path(topic, session).unlink(missing_ok=True)

    def _path(self, topic: str, session: str) -> Path:
        """Where this session's transcript file goes, under the store's root."""
        return self._root / _slug(topic) / f"{_slug(session)}.jsonl"


def _read_turns(path: Path) -> list[Turn]:
    """Load every turn from one transcript file, skipping what will not parse.

    A truncated last line is the ordinary way this happens: the process died
    mid-append. One bad line costs one turn, so the rest of the conversation
    still loads.
    """
    if not path.is_file():
        return []
    turns = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("transcript %s line %d is not JSON; skipped", path, number)
                continue
            if record.get("type") == _META_MARKER:
                continue
            turns.append(Turn.from_dict(record))
    return turns


def _summarize_transcript(path: Path) -> tuple[str, int, str | None] | None:
    """One transcript file as a listing needs it: session id, turn count, preview.

    None for a file that is not ours. The header line is what says so, and the
    session id it carries is the caller's original key — the slugged file name
    is not something a key can be reconstructed from.

    The whole file is read, because one turn is one line and a count of them has
    to reach the end. What keeps that affordable is what is skipped: lines are
    counted rather than parsed, and JSON is decoded only until the first turn a
    person submitted turns up, which is usually the line straight after the
    header. One consequence is worth knowing — a line too damaged for
    `_read_turns` to parse still counts as a turn here, so a transcript
    truncated by a killed process can report one turn more than it gives back.
    Agreeing with it exactly would mean parsing every line of every transcript
    under the topic each time a picker is drawn.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            name = _session_name_of(handle.readline())
            if name is None:
                return None
            turns = 0
            preview = None
            for line in handle:
                if not line.strip():
                    continue
                turns += 1
                if preview is None:
                    turn = _turn_of_line(line)
                    if turn is not None:
                        preview = preview_of((turn,))
            return name, turns, preview
    except OSError:
        return None


def _session_name_of(header: str) -> str | None:
    """The session id a transcript file's first line names, or None if it names none.

    A file whose first line is not one of our metadata records is not ours, and
    the caller skips it rather than guessing at a session id from the path.
    """
    try:
        record = json.loads(header)
    except json.JSONDecodeError:
        return None
    if record.get("type") != _META_MARKER:
        return None
    session = record.get("session")
    return session if isinstance(session, str) else None


def _turn_of_line(line: str) -> Turn | None:
    """One transcript line as a `Turn`, or None if it will not parse as one.

    Used where a bad line is worth stepping over rather than reporting, which
    `_read_turns` logs instead.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return Turn.from_dict(record) if isinstance(record, dict) else None


def _meta_record(topic: str, session: str) -> dict[str, Any]:
    """The header line that opens a transcript file."""
    return {"type": _META_MARKER, "topic": topic, "session": session, "written_at": time.time()}


def _slug(key: str) -> str:
    """A file-name-safe form of `key` that no other key can collide with.

    The readable part is `key` with everything outside `[A-Za-z0-9._-]`
    collapsed to dashes and trimmed to `_SLUG_TEXT_LIMIT`; a digest of the
    whole original key follows it.

    The digest is not decoration. Trimming and collapsing are both lossy —
    `bug 1/2` and `bug 1-2` reduce to the same text — and two topics sharing a
    transcript file is a conversation leaking into another one. It also settles
    the traversal question: whatever a host passes, `..` and `/` cannot survive
    into the path, and the digest keeps the result unique even after the
    stripping makes the visible part empty.
    """
    text = _SLUG_KEEP.sub("-", key).strip("-.")[:_SLUG_TEXT_LIMIT].strip("-.")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return f"{text}-{digest}" if text else digest
