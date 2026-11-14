"""Tests for the transcript read-check (``docs/architecture.md``, "What counts as read").

Each excluded record kind has a negative control, as the design requires: it
must count as unread, because counting it as read would be mail loss.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spanreed.sleepers import InboxPosition
from spanreed.transcript import (
    TranscriptStatus,
    find_transcript,
    read_deliveries,
    scan_inbox,
)

RECV = "mcp__plugin_spanreed_spanreed__recv_messages"


class Transcript:
    """Builds a transcript as a chain of records, each the child of the last."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._n = 0

    def _add(self, record: dict[str, Any]) -> str:
        self._n += 1
        uuid = f"u{self._n}"
        parent = self.records[-1]["uuid"] if self.records else None
        self.records.append({"uuid": uuid, "parentUuid": parent, **record})
        return uuid

    def notification(self, *event_lines: str, tag: str = "event") -> str:
        body = "\n".join(event_lines)
        text = (
            "<task-notification>\n<task-id>b1</task-id>\n<summary>Monitor event</summary>\n"
            f"<{tag}>{body}</{tag}>\n</task-notification>"
        )
        return self._add({"type": "user", "message": {"role": "user", "content": text}})

    def queued_command(self, *event_lines: str, attachment_type: str = "queued_command") -> str:
        prompt = (
            "<task-notification>\n<event>"
            + "\n".join(event_lines)
            + "</event>\n</task-notification>"
        )
        return self._add(
            {"type": "attachment", "attachment": {"type": attachment_type, "prompt": prompt}}
        )

    def enqueue(self, *event_lines: str) -> None:
        # queue-operation records carry no uuid in real transcripts.
        self.records.append(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "<event>" + "\n".join(event_lines) + "</event>",
            }
        )

    def tool_use(self, name: str, tool_id: str) -> str:
        return self._add(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5-5",
                    "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
                },
            }
        )

    def tool_result(self, tool_id: str, content: Any, **extra: Any) -> str:
        return self._add(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tool_id, "content": content}
                    ],
                },
                **extra,
            }
        )

    def reply(self) -> str:
        return self._add(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": "ok"}],
                },
            }
        )

    def synthetic(self, error: str | None) -> str:
        record: dict[str, Any] = {
            "type": "assistant",
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "x"}]},
        }
        if error:
            record |= {"error": error, "isApiErrorMessage": True}
        return self._add(record)

    def other(self, record: dict[str, Any]) -> str:
        return self._add(record)

    def write(self, path: Path) -> Path:
        path.write_text("".join(json.dumps(r) + "\n" for r in self.records))
        return path


def line(msg_id: str, body: str = "hello") -> str:
    """An inbox line exactly as the store writes it: ``msg_id`` first."""
    return json.dumps(
        {"msg_id": msg_id, "from_agent": "a", "to_agent": "b", "body": body}, separators=(",", ":")
    )


def recv_result(*ids: str) -> str:
    return json.dumps({"result": [json.loads(line(i)) for i in ids]})


@pytest.fixture
def t() -> Transcript:
    t = Transcript()
    t.other({"type": "user", "message": {"role": "user", "content": "start"}})
    t.reply()
    return t


def delivered(t: Transcript, tmp_path: Path) -> set[str]:
    result = read_deliveries(t.write(tmp_path / "s.jsonl"))
    assert result.status is TranscriptStatus.OK
    return result.delivered


class TestDeliveredKinds:
    def test_a_monitor_event_between_turns(self, t: Transcript, tmp_path: Path) -> None:
        t.notification(line("msg-1"))
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1"}

    def test_a_batched_event_delivers_every_line_in_it(self, t: Transcript, tmp_path: Path) -> None:
        t.notification(line("msg-1"), line("msg-2"), line("msg-3"))
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1", "msg-2", "msg-3"}

    def test_lines_cut_from_a_batch_are_not_delivered(self, t: Transcript, tmp_path: Path) -> None:
        """Test 9: a batch is cut at ~3 KB; the cut line's id is in no record."""
        t.notification(line("msg-1"), line("msg-2")[:14] + "...(truncated)")
        t.reply()
        # The second line was cut before its id closed: only a prefix survived.
        assert delivered(t, tmp_path) == {"msg-1"}

    def test_a_truncated_event_still_delivers_its_id(self, t: Transcript, tmp_path: Path) -> None:
        """Events are cut at ~514 chars; the id survives because it comes first."""
        t.notification(line("msg-1", body="x" * 2000)[:514])
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1"}

    def test_a_monitor_event_delivered_mid_turn(self, t: Transcript, tmp_path: Path) -> None:
        t.queued_command(line("msg-1"))
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1"}

    def test_a_recv_messages_result(self, t: Transcript, tmp_path: Path) -> None:
        t.tool_use(RECV, "tool-1")
        t.tool_result("tool-1", recv_result("msg-1", "msg-2"))
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1", "msg-2"}

    def test_a_recv_result_given_as_text_blocks(self, t: Transcript, tmp_path: Path) -> None:
        text = recv_result("msg-1")
        t.tool_use(RECV, "tool-1")
        t.tool_result(
            "tool-1", [{"type": "text", "text": text[:10]}, {"type": "text", "text": text[10:]}]
        )
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1"}

    def test_the_reply_may_come_several_records_later(self, t: Transcript, tmp_path: Path) -> None:
        t.notification(line("msg-1"))
        t.other({"type": "attachment", "attachment": {"type": "date_change"}})
        t.synthetic(None)
        t.reply()
        assert delivered(t, tmp_path) == {"msg-1"}


class TestNegativeControls:
    """Every one of these must count as unread."""

    def test_enqueue_only(self, t: Transcript, tmp_path: Path) -> None:
        """Loss E: the enqueue is written before the delivered record."""
        t.enqueue(line("msg-1"))
        assert delivered(t, tmp_path) == set()

    def test_a_tool_result_not_from_recv_messages(self, t: Transcript, tmp_path: Path) -> None:
        """An agent that prints its transcript or inbox produces exactly this."""
        t.tool_use("Bash", "tool-1")
        t.tool_result("tool-1", "<task-notification>\n<event>" + line("msg-1") + "</event>")
        t.tool_use("Bash", "tool-2")
        t.tool_result("tool-2", recv_result("msg-2"))
        t.reply()
        assert delivered(t, tmp_path) == set()

    def test_an_attachment_that_is_not_a_queued_command(
        self, t: Transcript, tmp_path: Path
    ) -> None:
        t.queued_command(line("msg-1"), attachment_type="edited_text_file")
        t.reply()
        assert delivered(t, tmp_path) == set()

    def test_an_over_limit_recv_result_with_ids_in_mcp_meta(
        self, t: Transcript, tmp_path: Path
    ) -> None:
        """Claude got only the error; the clean copy is in fields it never sees."""
        t.tool_use(RECV, "tool-1")
        t.tool_result(
            "tool-1",
            "Error: result (91,342 characters) exceeds maximum allowed tokens.",
            mcpMeta={"result": json.loads(recv_result("msg-1"))},
            toolUseResult=json.loads(recv_result("msg-1")),
        )
        t.reply()
        assert delivered(t, tmp_path) == set()

    def test_a_persisted_recv_preview(self, t: Transcript, tmp_path: Path) -> None:
        t.tool_use(RECV, "tool-1")
        t.tool_result("tool-1", recv_result("msg-1", "msg-2")[:60] + "\n... [preview]")
        t.reply()
        assert delivered(t, tmp_path) == set()

    @pytest.mark.parametrize("error", ["rate_limit", "authentication_failed", "server_error"])
    def test_a_notification_answered_only_by_an_api_error(
        self, t: Transcript, tmp_path: Path, error: str
    ) -> None:
        t.notification(line("msg-1"))
        t.synthetic(error)
        result = read_deliveries(t.write(tmp_path / "s.jsonl"))
        assert result.delivered == set()
        assert result.unanswered == {"msg-1"}
        assert result.error_turns == {error: 1}

    def test_a_recv_result_answered_only_by_an_api_error(
        self, t: Transcript, tmp_path: Path
    ) -> None:
        t.tool_use(RECV, "tool-1")
        t.tool_result("tool-1", recv_result("msg-1"))
        t.synthetic("rate_limit")
        assert delivered(t, tmp_path) == set()

    def test_a_notification_the_session_ended_on(self, t: Transcript, tmp_path: Path) -> None:
        t.notification(line("msg-1"))
        assert delivered(t, tmp_path) == set()

    def test_a_real_reply_on_another_branch_does_not_count(
        self, t: Transcript, tmp_path: Path
    ) -> None:
        """The reply must have the candidate in its parentUuid chain."""
        branch_point = t.records[-1]["uuid"]
        t.notification(line("msg-1"))
        t.synthetic("rate_limit")
        t.other(
            {
                "type": "assistant",
                "parentUuid": branch_point,
                "message": {"model": "claude-opus-5-5", "content": []},
            }
        )
        t.records[-1]["parentUuid"] = branch_point
        assert delivered(t, tmp_path) == set()

    def test_a_notification_answered_only_by_a_no_error_synthetic(
        self, t: Transcript, tmp_path: Path
    ) -> None:
        """The "No response requested." record after a SIGTERM resume: no error, and
        the model never ran. Unread, and not a real turn."""
        t.notification(line("msg-1"))
        t.synthetic(None)
        before = read_deliveries(tmp_path / "none.jsonl").real_turns
        result = read_deliveries(t.write(tmp_path / "s.jsonl"))
        assert result.delivered == set()
        assert result.real_turns == 1 + before, "only the fixture's opening reply"
        assert result.error_turns == {}

    def test_a_model_record_carrying_an_error_is_not_a_real_turn(
        self, t: Transcript, tmp_path: Path
    ) -> None:
        t.notification(line("msg-1"))
        t.other(
            {
                "type": "assistant",
                "error": "server_error",
                "isApiErrorMessage": True,
                "message": {"model": "claude-opus-5-5", "content": []},
            }
        )
        assert delivered(t, tmp_path) == set()

    def test_an_id_quoted_in_a_subagent_result(self, t: Transcript, tmp_path: Path) -> None:
        """Only <event> sections are read; a subagent's <result> is not a delivery."""
        t.notification(line("msg-1"), tag="result")
        t.reply()
        assert delivered(t, tmp_path) == set()

    def test_an_id_mentioned_mid_line(self, t: Transcript, tmp_path: Path) -> None:
        t.notification('reply to {"msg_id":"msg-1"} please')
        t.reply()
        assert delivered(t, tmp_path) == set()

    def test_an_id_the_agent_wrote_itself(self, t: Transcript, tmp_path: Path) -> None:
        t.other(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-5-5",
                    "content": [{"type": "text", "text": line("msg-1")}],
                },
            }
        )
        assert delivered(t, tmp_path) == set()


class TestStatus:
    def test_missing(self, tmp_path: Path) -> None:
        assert read_deliveries(tmp_path / "nope.jsonl").status is TranscriptStatus.MISSING
        assert read_deliveries(None).status is TranscriptStatus.MISSING

    def test_unreadable(self, tmp_path: Path) -> None:
        assert read_deliveries(tmp_path).status is TranscriptStatus.UNREADABLE

    def test_unrecognized_format_fails_safe(self, tmp_path: Path) -> None:
        """As if the format changed: nothing counts, and the caller is told why."""
        path = tmp_path / "s.jsonl"
        path.write_text(json.dumps({"kind": "turn", "uuid": "a", "text": line("msg-1")}) + "\n")
        result = read_deliveries(path)
        assert result.status is TranscriptStatus.UNRECOGNIZED
        assert not result.trustworthy
        assert result.delivered == set()

    def test_a_torn_last_line_is_ignored(self, t: Transcript, tmp_path: Path) -> None:
        t.notification(line("msg-1"))
        t.reply()
        path = t.write(tmp_path / "s.jsonl")
        with path.open("a") as f:
            f.write('{"type": "user", "uuid": "torn"')
        assert read_deliveries(path).delivered == {"msg-1"}

    def test_find_transcript_under_any_project(self, tmp_path: Path) -> None:
        sid = "6d1b9a5e-0000-4000-8000-000000000000"
        project = tmp_path / "projects" / "-home-x-repo"
        project.mkdir(parents=True)
        (project / f"{sid}.jsonl").write_text("")
        assert find_transcript(sid, tmp_path) == project / f"{sid}.jsonl"
        assert find_transcript("other", tmp_path) is None
        assert find_transcript(sid, tmp_path / "nothing") is None


class TestScanInbox:
    @pytest.fixture
    def inbox(self, tmp_path: Path) -> Path:
        path = tmp_path / "inbox.jsonl"
        path.write_text("".join(line(f"msg-{n}") + "\n" for n in range(1, 6)))
        return path

    def test_first_line_after_read_through_without_a_delivery(self, inbox: Path) -> None:
        scan = scan_inbox(inbox, InboxPosition(line=2, msg_id="msg-2"), {"msg-3"})
        assert scan.first_unread == 4
        assert (scan.end.line, scan.end.msg_id) == (5, "msg-5")
        assert not scan.rewritten

    def test_all_read(self, inbox: Path) -> None:
        scan = scan_inbox(inbox, InboxPosition(line=3, msg_id="msg-3"), {"msg-4", "msg-5"})
        assert scan.first_unread is None

    def test_lines_before_read_through_are_not_reconsidered(self, inbox: Path) -> None:
        scan = scan_inbox(inbox, InboxPosition(line=5, msg_id="msg-5"), set())
        assert scan.first_unread is None

    def test_a_corrupt_line_is_unread(self, tmp_path: Path) -> None:
        path = tmp_path / "inbox.jsonl"
        path.write_text(line("msg-1") + "\nnot json\n" + line("msg-3") + "\n")
        scan = scan_inbox(path, InboxPosition(), {"msg-1", "msg-3"})
        assert scan.first_unread == 2

    def test_a_rewritten_inbox_starts_over(self, inbox: Path) -> None:
        """Duplicates, never a loss."""
        scan = scan_inbox(inbox, InboxPosition(line=2, msg_id="msg-OTHER"), {"msg-1"})
        assert scan.rewritten
        assert scan.first_unread == 2

    def test_a_shrunken_inbox_starts_over(self, inbox: Path) -> None:
        scan = scan_inbox(inbox, InboxPosition(line=9, msg_id="msg-9"), set())
        assert scan.rewritten
        assert scan.first_unread == 1

    def test_no_inbox(self, tmp_path: Path) -> None:
        scan = scan_inbox(tmp_path / "none.jsonl", InboxPosition(), set())
        assert scan.first_unread is None
        assert scan.end.line == 0
