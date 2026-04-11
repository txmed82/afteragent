from pathlib import Path

from afteragent.transcripts import (
    KIND_ASSISTANT_MESSAGE,
    KIND_BASH_COMMAND,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PARSE_ERROR,
    KIND_TEST_RUN,
    KIND_TODO_UPDATE,
    KIND_USER_MESSAGE,
    SOURCE_CLAUDE_CODE_JSONL,
    parse_claude_code_jsonl,
)


FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code"


def test_claude_code_parser_handles_simple_edit_run():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)

    # Sequence is monotonic from 0.
    assert events[0].sequence == 0
    for i in range(1, len(events)):
        assert events[i].sequence == events[i - 1].sequence + 1

    kinds = [e.kind for e in events]
    assert KIND_USER_MESSAGE in kinds
    assert KIND_ASSISTANT_MESSAGE in kinds
    assert KIND_FILE_READ in kinds
    assert KIND_FILE_EDIT in kinds
    # The bash "pytest tests/test_foo.py -v" should be classified as test_run.
    assert KIND_TEST_RUN in kinds

    # All events tagged with the Claude Code source.
    assert all(e.source == SOURCE_CLAUDE_CODE_JSONL for e in events)


def test_claude_code_parser_extracts_read_target():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    reads = [e for e in events if e.kind == KIND_FILE_READ]
    assert len(reads) >= 1
    assert reads[0].target == "/repo/tests/test_foo.py"
    assert reads[0].tool_name == "Read"


def test_claude_code_parser_extracts_edit_target():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    edits = [e for e in events if e.kind == KIND_FILE_EDIT]
    assert len(edits) >= 1
    assert edits[0].target == "/repo/tests/test_foo.py"
    assert edits[0].tool_name == "Edit"


def test_claude_code_parser_classifies_pytest_as_test_run():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) >= 1
    assert "pytest" in (test_runs[0].target or "")


def test_claude_code_parser_skips_malformed_lines_and_emits_parse_error():
    text = (FIXTURES / "malformed.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    # At least one parse_error for the broken line, plus valid events still parsed.
    assert any(e.kind == KIND_PARSE_ERROR for e in events)
    assert any(e.kind == KIND_FILE_READ for e in events)


def test_claude_code_parser_handles_continued_session():
    text = (FIXTURES / "continued_session.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    assert any(e.kind == KIND_TODO_UPDATE for e in events)


def test_claude_code_parser_includes_line_number_raw_ref():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    # At least some events should have a raw_ref of form "line:N".
    with_refs = [e for e in events if e.raw_ref and e.raw_ref.startswith("line:")]
    assert len(with_refs) >= 1


def test_claude_code_parser_never_raises_on_completely_broken_input():
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text="\x00\x01not jsonl at all")
    assert isinstance(events, list)


def test_claude_code_parser_attaches_tool_results_to_tool_events():
    """Regression: tool_result content must land on the matching tool_use event,
    not be emitted as a separate user_message event. Tool_use and tool_result
    live in different JSONL records, so the attachment must happen across
    records via tool_use_id."""
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)

    # The Read tool_use event must carry the file content as its output_excerpt.
    reads = [e for e in events if e.kind == KIND_FILE_READ]
    assert len(reads) == 1
    read = reads[0]
    assert "def test_foo" in read.output_excerpt
    assert read.status == "success"

    # The Edit tool_use event must carry the edit confirmation.
    edits = [e for e in events if e.kind == KIND_FILE_EDIT]
    assert len(edits) == 1
    edit = edits[0]
    assert "File edited successfully" in edit.output_excerpt
    assert edit.status == "success"

    # The test_run (pytest) event must carry the pytest output and success status.
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) == 1
    test_run = test_runs[0]
    assert "1 passed" in test_run.output_excerpt
    assert test_run.status == "success"
