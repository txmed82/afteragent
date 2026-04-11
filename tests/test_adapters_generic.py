from pathlib import Path

from afteragent.transcripts import (
    KIND_TEST_RUN,
    KIND_UNKNOWN,
    SOURCE_STDOUT_HEURISTIC,
    parse_generic_stdout,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "generic"


def test_generic_parser_returns_empty_list_for_empty_stdout():
    events = parse_generic_stdout(run_id="abc", stdout="", stderr="")
    assert events == []


def test_generic_parser_detects_pytest_run_as_test_run():
    stdout = (FIXTURES / "pytest_output.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")

    # At least one test_run event should be emitted.
    test_events = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_events) >= 1
    # It should reference pytest and carry the failure signal.
    first = test_events[0]
    assert first.source == SOURCE_STDOUT_HEURISTIC
    assert first.status == "error"  # pytest output contains "FAILED" and "1 failed"
    assert first.sequence == 0 or first.sequence >= 0  # monotonic


def test_generic_parser_assigns_monotonic_sequences():
    stdout = (FIXTURES / "pytest_output.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert sequences[0] == 0


def test_generic_parser_tags_all_events_with_heuristic_source():
    stdout = (FIXTURES / "npm_script.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")
    for event in events:
        assert event.source == SOURCE_STDOUT_HEURISTIC


def test_generic_parser_never_raises_on_garbage():
    # Even nonsense input should produce a list, never an exception.
    events = parse_generic_stdout(run_id="abc", stdout="\x00\x01\x02", stderr="\xff" * 100)
    assert isinstance(events, list)


def test_generic_parser_detects_successful_npm_build():
    stdout = (FIXTURES / "npm_script.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")
    # At least one event should have status "success" given "compiled successfully".
    success_events = [e for e in events if e.status == "success"]
    assert len(success_events) >= 1
