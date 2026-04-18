from dataclasses import dataclass
from typing import Any

import pytest

from afteragent.diagnostics_generic import run_generic_detectors
from afteragent.models import PatternFinding, RunRecord


@dataclass
class _StubEvent:
    """Minimal stand-in for TranscriptEventRow used by the detectors."""
    kind: str
    target: str | None = None
    output_excerpt: str = ""


def _run_record(
    run_id: str = "run1",
    exit_code: int | None = 0,
    task_prompt: str | None = "do something",
) -> RunRecord:
    return RunRecord(
        id=run_id,
        command="some command",
        cwd="/tmp",
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        created_at="2026-04-11T12:00:00Z",
        finished_at="2026-04-11T12:00:01Z",
        duration_ms=1000,
        summary="ok",
        task_prompt=task_prompt,
    )


def _context(
    run: RunRecord | None = None,
    transcript_events: list[_StubEvent] | None = None,
    changed_files: set[str] | None = None,
) -> dict:
    return {
        "run": run or _run_record(),
        "transcript_events": transcript_events or [],
        "changed_files": changed_files or set(),
    }


# ---------- run_generic_detectors entry point ----------


def test_run_generic_detectors_empty_context_returns_empty_list():
    findings = run_generic_detectors(_context(), store=None)
    assert findings == []


def test_run_generic_detectors_isolates_detector_failures(monkeypatch):
    """If one detector raises, the others still run."""
    from afteragent import diagnostics_generic

    calls: list[str] = []

    def broken_detector(context, store):
        calls.append("broken")
        raise RuntimeError("simulated failure")

    def good_detector(context, store):
        calls.append("good")
        return PatternFinding(
            code="good",
            title="good finding",
            severity="low",
            summary="x",
            evidence=[],
        )

    monkeypatch.setattr(
        diagnostics_generic,
        "_DETECTORS",
        [broken_detector, good_detector],
    )

    findings = run_generic_detectors(_context(), store=None)
    assert calls == ["broken", "good"]
    assert len(findings) == 1
    assert findings[0].code == "good"


# ---------- agent_edits_without_tests ----------


def test_edits_without_tests_fires_when_diff_has_edits_and_no_test_run():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" in codes


def test_edits_without_tests_skipped_when_pytest_was_run():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="test_run", target="pytest tests/"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" not in codes


def test_edits_without_tests_skipped_when_bash_test_command_was_run():
    """A bash_command event whose target contains a test-runner pattern
    counts as a test run."""
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="bash_command", target="npm test --watch=false"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" not in codes


def test_edits_without_tests_skipped_when_no_edits_at_all():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/foo.py"),
        ],
        changed_files=set(),
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" not in codes


# ---------- agent_stuck_on_file ----------


def test_stuck_on_file_fires_at_threshold_of_four_edits():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    stuck = [f for f in findings if f.code == "agent_stuck_on_file"]
    assert len(stuck) == 1
    assert "foo.py" in stuck[0].title
    assert "4" in stuck[0].title


def test_stuck_on_file_resets_counter_on_test_run():
    """Test runs break the 'consecutive edits' streak."""
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="test_run", target="pytest"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    # Max streak is 2 (before test) and 2 (after test). Neither hits threshold 4.
    stuck = [f for f in findings if f.code == "agent_stuck_on_file"]
    assert stuck == []


def test_stuck_on_file_skipped_below_threshold():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    stuck = [f for f in findings if f.code == "agent_stuck_on_file"]
    assert stuck == []


# ---------- agent_read_edit_divergence ----------


def test_read_edit_divergence_fires_when_zero_overlap():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_read", target="/repo/b.py"),
            _StubEvent(kind="file_edit", target="/repo/c.py"),
        ],
        changed_files={"c.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_read_edit_divergence" in codes


def test_read_edit_divergence_skipped_when_files_overlap():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_read", target="/repo/b.py"),
            _StubEvent(kind="file_edit", target="/repo/a.py"),
        ],
        changed_files={"a.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_read_edit_divergence" not in codes


def test_read_edit_divergence_skipped_below_activity_threshold():
    """Needs at least 2 reads AND 1 edit."""
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_edit", target="/repo/c.py"),
        ],
        changed_files={"c.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_read_edit_divergence" not in codes


# ---------- agent_command_failure_hidden ----------


def test_command_failure_hidden_fires_on_nonzero_exit_with_success_claim():
    ctx = _context(
        run=_run_record(exit_code=1),
        transcript_events=[
            _StubEvent(
                kind="assistant_message",
                output_excerpt="All tests passing, task is done.",
            ),
        ],
    )
    findings = run_generic_detectors(ctx, store=None)
    hidden = [f for f in findings if f.code == "agent_command_failure_hidden"]
    assert len(hidden) == 1
    assert "1" in hidden[0].title


def test_command_failure_hidden_skipped_on_zero_exit():
    ctx = _context(
        run=_run_record(exit_code=0),
        transcript_events=[
            _StubEvent(
                kind="assistant_message",
                output_excerpt="All tests passing.",
            ),
        ],
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_command_failure_hidden" not in codes


def test_command_failure_hidden_skipped_when_no_success_claim():
    ctx = _context(
        run=_run_record(exit_code=1),
        transcript_events=[
            _StubEvent(
                kind="assistant_message",
                output_excerpt="I hit an error and I'm not sure what to do.",
            ),
        ],
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_command_failure_hidden" not in codes


# ---------- agent_zero_meaningful_activity ----------


def test_zero_meaningful_activity_fires_on_minimal_events_and_empty_diff():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/foo.py"),
        ],
        changed_files=set(),
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_zero_meaningful_activity" in codes


def test_zero_meaningful_activity_skipped_when_activity_threshold_met():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_read", target="/repo/b.py"),
            _StubEvent(kind="file_read", target="/repo/c.py"),
        ],
        changed_files=set(),
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_zero_meaningful_activity" not in codes


def test_zero_meaningful_activity_skipped_when_diff_has_changes():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
        ],
        changed_files={"a.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_zero_meaningful_activity" not in codes
