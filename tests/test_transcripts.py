import tempfile
from pathlib import Path

from afteragent.config import resolve_paths
from afteragent.store import Store
from afteragent.transcripts import (
    INPUTS_SUMMARY_MAX,
    KIND_ASSISTANT_MESSAGE,
    KIND_BASH_COMMAND,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_HOOK_EVENT,
    KIND_PARSE_ERROR,
    KIND_SEARCH,
    KIND_SUBAGENT_CALL,
    KIND_TEST_RUN,
    KIND_TODO_UPDATE,
    KIND_UNKNOWN,
    KIND_USER_MESSAGE,
    KIND_WEB_FETCH,
    OUTPUT_EXCERPT_MAX,
    SOURCE_CLAUDE_CODE_JSONL,
    SOURCE_CODEX_STDOUT,
    SOURCE_STDOUT_HEURISTIC,
    TranscriptEvent,
    make_parse_error,
    truncate,
)


def test_transcript_event_minimal_construction():
    event = TranscriptEvent(
        run_id="abc123",
        sequence=0,
        kind=KIND_FILE_READ,
        tool_name="Read",
        target="/repo/README.md",
        inputs_summary="",
        output_excerpt="",
        status="success",
        source=SOURCE_CLAUDE_CODE_JSONL,
        timestamp="2026-04-10T12:00:00Z",
        raw_ref="line:42",
    )
    assert event.run_id == "abc123"
    assert event.sequence == 0
    assert event.kind == "file_read"
    assert event.tool_name == "Read"
    assert event.target == "/repo/README.md"
    assert event.status == "success"
    assert event.source == "claude_code_jsonl"


def test_transcript_event_allows_optional_fields_as_none():
    event = TranscriptEvent(
        run_id="abc123",
        sequence=1,
        kind=KIND_ASSISTANT_MESSAGE,
        tool_name=None,
        target=None,
        inputs_summary="",
        output_excerpt="I'm going to fix the failing test.",
        status="unknown",
        source=SOURCE_CLAUDE_CODE_JSONL,
        timestamp="",
        raw_ref=None,
    )
    assert event.tool_name is None
    assert event.target is None
    assert event.raw_ref is None


def test_event_kind_constants_have_expected_values():
    assert KIND_FILE_READ == "file_read"
    assert KIND_FILE_EDIT == "file_edit"
    assert KIND_BASH_COMMAND == "bash_command"
    assert KIND_TEST_RUN == "test_run"
    assert KIND_SEARCH == "search"
    assert KIND_WEB_FETCH == "web_fetch"
    assert KIND_TODO_UPDATE == "todo_update"
    assert KIND_SUBAGENT_CALL == "subagent_call"
    assert KIND_ASSISTANT_MESSAGE == "assistant_message"
    assert KIND_USER_MESSAGE == "user_message"
    assert KIND_HOOK_EVENT == "hook_event"
    assert KIND_PARSE_ERROR == "parse_error"
    assert KIND_UNKNOWN == "unknown"


def test_source_constants_have_expected_values():
    assert SOURCE_CLAUDE_CODE_JSONL == "claude_code_jsonl"
    assert SOURCE_CODEX_STDOUT == "codex_stdout"
    assert SOURCE_STDOUT_HEURISTIC == "stdout_heuristic"


def test_truncate_leaves_short_text_unchanged():
    assert truncate("hello world", 20) == "hello world"


def test_truncate_clips_long_text_with_ellipsis():
    text = "a" * 300
    result = truncate(text, 100)
    assert len(result) == 100
    assert result.endswith("…")
    assert result[:99] == "a" * 99


def test_truncate_handles_zero_length_safely():
    assert truncate("", 100) == ""


def test_truncate_handles_exact_length():
    text = "a" * 100
    assert truncate(text, 100) == text


def test_inputs_summary_max_is_200():
    assert INPUTS_SUMMARY_MAX == 200


def test_output_excerpt_max_is_500():
    assert OUTPUT_EXCERPT_MAX == 500


def test_make_parse_error_fills_all_required_fields():
    event = make_parse_error(
        run_id="abc123",
        sequence=5,
        source="claude_code_jsonl",
        message="could not decode JSON on line 42",
        raw_ref="line:42",
    )
    assert event.run_id == "abc123"
    assert event.sequence == 5
    assert event.kind == "parse_error"
    assert event.tool_name is None
    assert event.target is None
    assert event.inputs_summary == ""
    assert event.output_excerpt == "could not decode JSON on line 42"
    assert event.status == "error"
    assert event.source == "claude_code_jsonl"
    assert event.timestamp == ""
    assert event.raw_ref == "line:42"


def test_make_parse_error_truncates_long_messages():
    long_message = "x" * 1000
    event = make_parse_error(
        run_id="abc",
        sequence=0,
        source="stdout_heuristic",
        message=long_message,
        raw_ref=None,
    )
    assert len(event.output_excerpt) == OUTPUT_EXCERPT_MAX
    assert event.output_excerpt.endswith("…")


def _make_store(tmp: Path) -> Store:
    paths = resolve_paths(tmp)
    return Store(paths)


def _make_event(run_id: str, sequence: int, kind: str, target: str) -> TranscriptEvent:
    return TranscriptEvent(
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        tool_name="Read" if kind == KIND_FILE_READ else "Edit",
        target=target,
        inputs_summary="",
        output_excerpt="",
        status="success",
        source=SOURCE_CLAUDE_CODE_JSONL,
        timestamp="2026-04-10T12:00:00Z",
        raw_ref=None,
    )


def test_store_adds_and_retrieves_transcript_events_in_order():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        events = [
            _make_event("run1", 0, KIND_FILE_READ, "/repo/a.py"),
            _make_event("run1", 1, KIND_FILE_EDIT, "/repo/a.py"),
            _make_event("run1", 2, KIND_FILE_READ, "/repo/b.py"),
        ]
        store.add_transcript_events("run1", events)

        retrieved = store.get_transcript_events("run1")
        assert len(retrieved) == 3
        assert [e.sequence for e in retrieved] == [0, 1, 2]
        assert [e.kind for e in retrieved] == [
            "file_read",
            "file_edit",
            "file_read",
        ]
        assert retrieved[0].target == "/repo/a.py"


def test_store_filters_transcript_events_by_kind():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        events = [
            _make_event("run1", 0, KIND_FILE_READ, "/repo/a.py"),
            _make_event("run1", 1, KIND_FILE_EDIT, "/repo/a.py"),
            _make_event("run1", 2, KIND_FILE_READ, "/repo/b.py"),
        ]
        store.add_transcript_events("run1", events)

        reads = store.get_transcript_events("run1", kind="file_read")
        assert len(reads) == 2
        assert all(e.kind == "file_read" for e in reads)


def test_store_returns_empty_list_for_run_with_no_transcript_events():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        assert store.get_transcript_events("run1") == []


def test_store_handles_empty_event_list_in_add():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        store.add_transcript_events("run1", [])
        assert store.get_transcript_events("run1") == []


def test_store_add_transcript_events_uses_method_run_id_not_event_run_id():
    """Regression: add_transcript_events must use the method parameter run_id
    for the row's run_id column, not event.run_id. If a caller builds events
    with a stale or mismatched run_id field and then passes a different run_id
    to the method, the events must land under the caller's specified run_id —
    the method parameter is authoritative.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run_target", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        store.create_run("run_other", "echo hi", str(tmp), "2026-04-10T12:00:00Z")

        # Build events whose run_id field points at run_other, but pass
        # run_target as the method parameter. After the fix, events should
        # land under run_target — not run_other.
        events = [
            _make_event("run_other", 0, KIND_FILE_READ, "/repo/a.py"),
            _make_event("run_other", 1, KIND_FILE_EDIT, "/repo/b.py"),
        ]
        store.add_transcript_events("run_target", events)

        # Events landed under the method parameter run_id.
        target_events = store.get_transcript_events("run_target")
        assert len(target_events) == 2
        assert all(e.run_id == "run_target" for e in target_events)

        # The other run received nothing.
        other_events = store.get_transcript_events("run_other")
        assert other_events == []
