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


def test_codex_parser_zero_failed_is_not_an_error():
    """Regression: '0 failed' in the lookahead used to substring-match 'FAILED'
    and incorrectly mark passing runs as errors. A '0 failed' summary is
    affirmatively a passing run and must produce status='unknown', not 'error'.
    """
    stdout = (
        "codex: starting task\n"
        "codex: running `pytest tests/`\n"
        "============================= test session starts ==============================\n"
        "collected 3 items\n"
        "\n"
        "tests/test_foo.py ..                                                    [100%]\n"
        "\n"
        "============================== 3 passed, 0 failed in 0.02s ==============================\n"
        "codex: task complete\n"
    )
    events = parse_codex_stdout(run_id="r1", stdout=stdout, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) == 1
    assert test_runs[0].status != "error"


def test_codex_parser_nonzero_failed_is_an_error():
    """Regression companion: '1 failed' must flip status to error."""
    stdout = (
        "codex: running `pytest tests/`\n"
        "tests/test_foo.py F\n"
        "======================= 2 passed, 1 failed in 0.05s =======================\n"
    )
    events = parse_codex_stdout(run_id="r1", stdout=stdout, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) == 1
    assert test_runs[0].status == "error"


def test_codex_parser_standalone_fail_token_is_an_error():
    """When there is no 'N failed' count but a standalone FAIL/FAILED/ERROR
    token appears, that still counts as a failure (e.g. go test's 'FAIL' exit
    line, ruby rspec bare ERROR)."""
    stdout = (
        "codex: running `go test ./...`\n"
        "FAIL    example.com/app    0.01s\n"
        "FAIL\n"
        "exit status 1\n"
    )
    events = parse_codex_stdout(run_id="r1", stdout=stdout, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) == 1
    assert test_runs[0].status == "error"


def test_codex_parser_failed_substring_in_word_is_not_an_error():
    """Word-boundary check: a word like 'failedly' should not trigger failure,
    nor should 'FAILED' inside a longer identifier like 'ALLOWED_FAILED_LIST'."""
    stdout = (
        "codex: running `pytest tests/`\n"
        "some diagnostic output that happens to contain the word failedly\n"
        "======================= 1 passed in 0.01s =======================\n"
    )
    events = parse_codex_stdout(run_id="r1", stdout=stdout, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) == 1
    # "failedly" is a word, "failed" is a word boundary inside it, so the regex
    # will match. That's acceptable — the point of the test is that the
    # "0 failed" → not-error case still works, and the standalone "FAIL" →
    # error case still works. This test documents the boundary where
    # ambiguous prose could still trip the detector; it's the accepted
    # trade-off for a medium-fidelity parser.
    # For this case, the detector will NOT flip to error because there is
    # no \b(\d+)\s+failed\b match AND no standalone FAIL/FAILED/ERROR token.
    # The word "failedly" does not match \bfailed\b (which requires a word
    # boundary after 'd'). So status remains "unknown".
    assert test_runs[0].status == "unknown"
