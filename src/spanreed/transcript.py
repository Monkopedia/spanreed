"""Which bus messages a Claude Code session actually received, from its transcript.

``docs/architecture.md``, "What counts as read". A sleeper's mail counts as
read only when a **delivered record** in the session's transcript carries its
``msg_id`` **and the model ran on it**. Everything else, including an id that is
merely somewhere in the file, counts as unread. The transcript is Claude Code's
internal format, not a promised API, so every way of failing to read it fails
toward *unread*: a duplicate, never a loss.

The three delivered kinds:

1. a ``user`` record whose ``message.content`` is a string starting with
   ``<task-notification>`` (a Monitor event delivered between turns);
2. an ``attachment`` record with ``type == "queued_command"`` (one delivered
   mid-turn), read from its ``prompt``;
3. the ``tool_result`` block paired by ``tool_use_id`` with a ``tool_use`` of the
   spanreed ``recv_messages`` tool, read **only** from that block's ``content``
   and only when it is complete ``{"result": [...]}`` JSON. Never
   ``toolUseResult`` or ``mcpMeta``, which Claude never sees.

In 1 and 2 the ids are read only from inside ``<event>...</event>``, one per
inbox line, and only where a line starts with ``{"msg_id":"``: the inbox writes
``msg_id`` first so that it survives the Monitor's truncation of an event.

"The model ran on it" means a non-synthetic assistant record (``model`` is not
``"<synthetic>"`` and it carries no ``error``) has the candidate record in its
``parentUuid`` chain. A delivered record answered only by synthetic API-error
records (rate limit, expired login, server error) was never seen by the model.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import cast

from spanreed.sleepers import InboxPosition

RECV_TOOL_SUFFIX = "recv_messages"
"""``recv_messages`` as the plugin's MCP server exposes it, under any server prefix
(``mcp__plugin_spanreed_spanreed__recv_messages`` today)."""

_EVENT_RE = re.compile(r"<event>(.*?)(?:</event>|\Z)", re.DOTALL)
_EVENT_LINE_ID_RE = re.compile(r'^\{"msg_id":"([^"\\]+)"', re.MULTILINE)


class TranscriptStatus(Enum):
    OK = "ok"
    """Parsed, and it has the conversation records this module knows."""
    MISSING = "missing"
    """No file. Expected before a sleeper's first launch; otherwise suspicious."""
    UNREADABLE = "unreadable"
    """The file exists but could not be read."""
    UNRECOGNIZED = "unrecognized"
    """Read, but holds no ``user``/``assistant`` record of the known shape: as if
    the format had changed under us."""


@dataclass
class Deliveries:
    """What one pass over a transcript found."""

    status: TranscriptStatus
    delivered: set[str] = field(default_factory=set[str])
    """``msg_id``\\ s with a delivered record the model ran on."""
    unanswered: set[str] = field(default_factory=set[str])
    """``msg_id``\\ s whose only delivered records were never followed by a real
    model turn (an API error, or the session ending first). Diagnostic only;
    they are unread."""
    real_turns: int = 0
    """Non-synthetic assistant records: evidence the model ran in this transcript."""
    error_turns: dict[str, int] = field(default_factory=dict[str, int])
    """Synthetic assistant records carrying an ``error``, counted by that error."""

    @property
    def trustworthy(self) -> bool:
        """Whether "not delivered" can be believed. False means fail safe."""
        return self.status is TranscriptStatus.OK


def claude_config_dir() -> Path:
    """Where Claude Code keeps its state: ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def find_transcript(session_id: str, config_dir: Path | None = None) -> Path | None:
    """The transcript of ``session_id``, under any project directory.

    Claude Code names the project directory after the cwd, with an encoding
    this module does not want to depend on. Session ids are UUIDs, so a match
    by file name alone is unambiguous.
    """
    projects = (config_dir or claude_config_dir()) / "projects"
    if not projects.is_dir():
        return None
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _event_ids(text: str) -> list[str]:
    """``msg_id``\\ s of the inbox lines inside the ``<event>`` sections of ``text``."""
    ids: list[str] = []
    for event in _EVENT_RE.findall(text):
        ids.extend(_EVENT_LINE_ID_RE.findall(event.lstrip("\n")))
    return ids


def _get(obj: object, key: str) -> object:
    """``obj[key]`` if ``obj`` is a JSON object, else ``None``. The transcript is
    untrusted JSON; every access goes through this or :func:`_items`."""
    return cast(dict[str, object], obj).get(key) if isinstance(obj, dict) else None


def _items(obj: object) -> list[object]:
    """``obj`` if it is a JSON array, else empty."""
    return cast(list[object], obj) if isinstance(obj, list) else []


def _recv_ids(content: object) -> list[str] | None:
    """Ids in a ``recv_messages`` result as Claude received it, or ``None`` when the
    text is not complete ``{"result": [...]}`` JSON (an over-limit error, a
    persisted-output preview, anything else)."""
    if not isinstance(content, str):
        # The API also allows a list of text blocks; join them.
        parts = [_get(block, "text") for block in _items(content)]
        texts = [part for part in parts if isinstance(part, str)]
        if not parts or len(texts) != len(parts):
            return None
        content = "".join(texts)
    try:
        parsed: object = json.loads(content)
    except json.JSONDecodeError:
        return None
    messages = _get(parsed, "result")
    items = _items(messages)
    if not isinstance(messages, list):
        return None
    ids = [_get(message, "msg_id") for message in items]
    return [msg_id for msg_id in ids if isinstance(msg_id, str)]


def _is_real_assistant(record: object) -> bool:
    model = _get(_get(record, "message"), "model")
    return (
        isinstance(model, str)
        and model != "<synthetic>"
        and not _get(record, "error")
        and not _get(record, "isApiErrorMessage")
    )


def read_deliveries(path: Path | None) -> Deliveries:
    """One pass over the transcript at ``path``. ``None`` reads as missing."""
    if path is None:
        return Deliveries(TranscriptStatus.MISSING)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return Deliveries(TranscriptStatus.MISSING)
    except OSError:
        return Deliveries(TranscriptStatus.UNREADABLE)

    records: list[object] = []
    for line in raw.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line, or garbage: it delivers nothing

    parent: dict[str, str | None] = {}
    recv_uses: set[str] = set()
    candidates: list[tuple[str, list[str]]] = []  # (record uuid, ids)
    real_leaves: list[str] = []
    result = Deliveries(TranscriptStatus.OK)
    recognized = False

    for record in records:
        uuid = _get(record, "uuid")
        if not isinstance(uuid, str):
            continue
        parent_uuid = _get(record, "parentUuid")
        parent[uuid] = parent_uuid if isinstance(parent_uuid, str) else None
        kind = _get(record, "type")
        message = _get(record, "message")
        content = _get(message, "content")

        if kind == "assistant" and isinstance(message, dict):
            recognized = True
            error = _get(record, "error")
            if _is_real_assistant(record):
                result.real_turns += 1
                real_leaves.append(uuid)
            elif isinstance(error, str):
                result.error_turns[error] = result.error_turns.get(error, 0) + 1
            for block in _items(content):
                name = _get(block, "name")
                tool_id = _get(block, "id")
                if (
                    _get(block, "type") == "tool_use"
                    and isinstance(name, str)
                    and name.endswith(RECV_TOOL_SUFFIX)
                    and isinstance(tool_id, str)
                ):
                    recv_uses.add(tool_id)
        elif kind == "user" and isinstance(message, dict):
            recognized = True
            if isinstance(content, str) and content.startswith("<task-notification>"):
                candidates.append((uuid, _event_ids(content)))
            else:
                ids: list[str] = []
                for block in _items(content):
                    if (
                        _get(block, "type") == "tool_result"
                        and _get(block, "tool_use_id") in recv_uses
                    ):
                        ids.extend(_recv_ids(_get(block, "content")) or [])
                if ids:
                    candidates.append((uuid, ids))
        elif kind == "attachment":
            attachment = _get(record, "attachment")
            prompt = _get(attachment, "prompt")
            if _get(attachment, "type") == "queued_command" and isinstance(prompt, str):
                candidates.append((uuid, _event_ids(prompt)))

    if not recognized:
        return Deliveries(TranscriptStatus.UNRECOGNIZED)

    # Every ancestor of a real assistant record was seen by the model. Walk each
    # chain up until it reaches a record already marked, so the whole pass is
    # linear in the transcript, not in its depth times its length.
    answered: set[str] = set()
    for leaf in real_leaves:
        node = parent.get(leaf)
        while node is not None and node not in answered:
            answered.add(node)
            node = parent.get(node)

    for uuid, ids in candidates:
        (result.delivered if uuid in answered else result.unanswered).update(ids)
    result.unanswered -= result.delivered
    return result


@dataclass
class InboxScan:
    """Where reading stands in one inbox."""

    first_unread: int | None
    """1-based line number of the first unread line, or ``None`` if all are read."""
    end: InboxPosition
    """The inbox's last line, as of this scan."""
    rewritten: bool = False
    """``read_through`` no longer matched the inbox, so the scan started at line 1."""


def scan_inbox(inbox: Path, read_through: InboxPosition, delivered: set[str]) -> InboxScan:
    """Find the first line after ``read_through`` whose ``msg_id`` was not delivered.

    A line that is not JSON, or has no ``msg_id``, is unread: it cannot have a
    delivered record, and the poison bound (not this function) decides when to
    give up on it. If the line ``read_through`` names now carries a different id,
    the inbox was rewritten under it, and the scan starts over from line 1:
    duplicates, never a loss.
    """
    lines: list[str | None] = []
    if inbox.exists():
        with inbox.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    parsed: object = json.loads(line)
                except json.JSONDecodeError:
                    parsed = None
                msg_id = _get(parsed, "msg_id")
                lines.append(msg_id if isinstance(msg_id, str) else None)

    start = read_through.line
    rewritten = False
    if start > len(lines) or (start > 0 and lines[start - 1] != read_through.msg_id):
        start, rewritten = 0, True

    first_unread = next(
        (n for n in range(start + 1, len(lines) + 1) if lines[n - 1] not in delivered),
        None,
    )
    end = InboxPosition(line=len(lines), msg_id=lines[-1] if lines else None)
    return InboxScan(first_unread=first_unread, end=end, rewritten=rewritten)
