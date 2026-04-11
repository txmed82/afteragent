from afteragent.transcripts import (
    TranscriptEvent,
    KIND_FILE_READ,
    KIND_FILE_EDIT,
    KIND_BASH_COMMAND,
    KIND_TEST_RUN,
    KIND_SEARCH,
    KIND_WEB_FETCH,
    KIND_TODO_UPDATE,
    KIND_SUBAGENT_CALL,
    KIND_ASSISTANT_MESSAGE,
    KIND_USER_MESSAGE,
    KIND_HOOK_EVENT,
    KIND_PARSE_ERROR,
    KIND_UNKNOWN,
    SOURCE_CLAUDE_CODE_JSONL,
    SOURCE_CODEX_STDOUT,
    SOURCE_STDOUT_HEURISTIC,
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


from afteragent.transcripts import (
    truncate,
    make_parse_error,
    INPUTS_SUMMARY_MAX,
    OUTPUT_EXCERPT_MAX,
)


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
