from pathlib import Path

from afteragent.transcripts import (
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_TEST_RUN,
    SOURCE_CODEX_STDOUT,
    parse_codex_stdout,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "codex"


def test_codex_parser_extracts_reads_and_edits():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")

    reads = [e for e in events if e.kind == KIND_FILE_READ]
    edits = [e for e in events if e.kind == KIND_FILE_EDIT]
    assert len(reads) >= 1
    assert len(edits) >= 1
    assert reads[0].target == "/repo/tests/test_foo.py"
    assert edits[0].target == "/repo/tests/test_foo.py"


def test_codex_parser_detects_test_runs():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) >= 1
    assert "pytest" in (test_runs[0].target or "")


def test_codex_parser_marks_failed_tests():
    text = (FIXTURES / "test_run_with_errors.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    # At least one test run should carry error status.
    assert any(e.status == "error" for e in test_runs)


def test_codex_parser_tags_all_events_with_codex_source():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    assert all(e.source == SOURCE_CODEX_STDOUT for e in events)


def test_codex_parser_monotonic_sequences():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)


def test_codex_parser_never_raises():
    events = parse_codex_stdout(run_id="r1", stdout="\x00\x01nonsense", stderr="")
    assert isinstance(events, list)
