from __future__ import annotations

import re

from .models import PatternFinding
from .store import Store

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STUCK_FILE_EDIT_THRESHOLD = 4
_MIN_MEANINGFUL_EVENTS = 3

_TEST_COMMAND_PATTERNS = (
    re.compile(r"\bpytest\b"),
    re.compile(r"\bpython\s+-m\s+pytest\b"),
    re.compile(r"\bjest\b"),
    re.compile(r"\bnpm\s+(?:run\s+)?test\b"),
    re.compile(r"\byarn\s+test\b"),
    re.compile(r"\bgo\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\bmocha\b"),
    re.compile(r"\bvitest\b"),
    re.compile(r"\brspec\b"),
    re.compile(r"\bbundle\s+exec\s+rspec\b"),
    re.compile(r"\btox\b"),
    re.compile(r"\bunittest\b"),
)

_SUCCESS_CLAIM_PATTERNS = (
    re.compile(r"\b(fixed|done|complete|completed|ready|success(?:ful)?)\b", re.I),
    re.compile(r"\ball\s+tests\s+pass(?:ing)?\b", re.I),
    re.compile(r"\bfinished\b", re.I),
    re.compile(r"\bshould\s+(?:work|be)\s+(?:now|ready|good)\b", re.I),
)

_MEANINGFUL_KINDS = frozenset({
    "file_read",
    "file_edit",
    "bash_command",
    "test_run",
    "search",
    "web_fetch",
    "todo_update",
    "subagent_call",
})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_generic_detectors(
    context: dict,
    store: Store | None,
) -> list[PatternFinding]:
    """Run all 5 generic (non-PR) detectors against a run's context.

    Each detector is a pure function returning 0 or 1 PatternFinding.
    Per-detector try/except isolates failures so a buggy detector can't
    crash the pipeline.
    """
    findings: list[PatternFinding] = []
    for detector in _DETECTORS:
        try:
            result = detector(context, store)
        except Exception:
            continue
        if result is not None:
            findings.append(result)
    return findings


# ---------------------------------------------------------------------------
# Detector 1: agent_edits_without_tests
# ---------------------------------------------------------------------------


def _detect_edits_without_tests(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    diff_has_edits = bool(context.get("changed_files"))
    if not diff_has_edits and not _any_event_kind(transcript_events, "file_edit"):
        return None

    for event in transcript_events:
        if event.kind == "test_run":
            return None
        if event.kind == "bash_command":
            target = event.target or ""
            if any(p.search(target) for p in _TEST_COMMAND_PATTERNS):
                return None

    changed_files = sorted(context.get("changed_files") or set())
    return PatternFinding(
        code="agent_edits_without_tests",
        title="Agent edited files but never ran tests",
        severity="medium",
        summary=(
            "The agent modified one or more files during this run but did not "
            "execute any test command. The change landed unverified — the next "
            "run should confirm the edits behave as intended."
        ),
        evidence=[
            f"Changed files: {', '.join(changed_files[:5]) or 'none in diff but edit events present'}",
            f"Transcript events: {len(transcript_events)}",
        ],
    )


# ---------------------------------------------------------------------------
# Detector 2: agent_stuck_on_file
# ---------------------------------------------------------------------------


def _detect_stuck_on_file(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    if not transcript_events:
        return None

    edits_by_file: dict[str, int] = {}
    max_streak: dict[str, int] = {}

    for event in transcript_events:
        if event.kind == "test_run":
            edits_by_file.clear()
            continue
        if event.kind == "file_edit" and event.target:
            path = event.target
            edits_by_file[path] = edits_by_file.get(path, 0) + 1
            if edits_by_file[path] > max_streak.get(path, 0):
                max_streak[path] = edits_by_file[path]

    stuck_files = {
        path: count
        for path, count in max_streak.items()
        if count >= _STUCK_FILE_EDIT_THRESHOLD
    }
    if not stuck_files:
        return None

    path, count = max(stuck_files.items(), key=lambda pair: pair[1])
    all_stuck = sorted(stuck_files.items(), key=lambda pair: -pair[1])
    return PatternFinding(
        code="agent_stuck_on_file",
        title=f"Agent edited {path} {count} times without running tests",
        severity="high",
        summary=(
            f"The agent made {count} consecutive edits to {path} without a test "
            f"run between them. This is usually a sign of a stuck edit loop — "
            f"the agent is making guesses without measuring the outcome."
        ),
        evidence=[
            f"{stuck_path}: {stuck_count} consecutive edits"
            for stuck_path, stuck_count in all_stuck[:5]
        ],
    )


# ---------------------------------------------------------------------------
# Detector 3: agent_read_edit_divergence
# ---------------------------------------------------------------------------


def _detect_read_edit_divergence(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    if not transcript_events:
        return None

    read_paths: set[str] = set()
    edit_paths: set[str] = set()
    for event in transcript_events:
        if event.kind == "file_read" and event.target:
            read_paths.add(event.target)
        elif event.kind == "file_edit" and event.target:
            edit_paths.add(event.target)

    if len(read_paths) < 2 or len(edit_paths) < 1:
        return None

    overlap = read_paths & edit_paths
    if overlap:
        return None

    return PatternFinding(
        code="agent_read_edit_divergence",
        title="Agent read files it never edited and edited files it never read",
        severity="medium",
        summary=(
            f"The agent read {len(read_paths)} files and edited {len(edit_paths)} "
            f"files, but there's zero overlap between the two sets. Typically "
            f"this means the agent got oriented on one part of the codebase and "
            f"then made changes somewhere unrelated."
        ),
        evidence=[
            f"Read (not edited): {', '.join(sorted(read_paths)[:5])}",
            f"Edited (not read): {', '.join(sorted(edit_paths)[:5])}",
        ],
    )


# ---------------------------------------------------------------------------
# Detector 4: agent_command_failure_hidden
# ---------------------------------------------------------------------------


def _detect_command_failure_hidden(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
    run = context.get("run")
    if run is None or run.exit_code in (None, 0):
        return None

    transcript_events = context.get("transcript_events") or []
    final_assistant_message: str | None = None
    for event in reversed(transcript_events):
        if event.kind == "assistant_message" and event.output_excerpt:
            final_assistant_message = event.output_excerpt
            break

    if final_assistant_message is None:
        return None

    if not any(p.search(final_assistant_message) for p in _SUCCESS_CLAIM_PATTERNS):
        return None

    return PatternFinding(
        code="agent_command_failure_hidden",
        title=f"Agent claimed success but process exited with code {run.exit_code}",
        severity="high",
        summary=(
            f"The run exited with code {run.exit_code}, but the agent's final "
            f"assistant message sounds like a success claim. The next run should "
            f"explicitly verify the task is actually done — the current state is "
            f"lying about its own outcome."
        ),
        evidence=[
            f"Exit code: {run.exit_code}",
            f"Final assistant message: {final_assistant_message[:200]}",
        ],
    )


# ---------------------------------------------------------------------------
# Detector 5: agent_zero_meaningful_activity
# ---------------------------------------------------------------------------


def _detect_zero_meaningful_activity(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    changed_files = context.get("changed_files") or set()

    meaningful_count = sum(
        1 for e in transcript_events if e.kind in _MEANINGFUL_KINDS
    )

    if meaningful_count == 0:
        return None
    if meaningful_count >= _MIN_MEANINGFUL_EVENTS:
        return None
    if len(changed_files) >= 1:
        return None

    run = context.get("run")
    return PatternFinding(
        code="agent_zero_meaningful_activity",
        title="Agent produced minimal activity and no code changes",
        severity="medium",
        summary=(
            f"The agent performed only {meaningful_count} meaningful tool "
            f"invocation(s) and the diff is empty. The run either hit an early "
            f"error, the agent was confused about the task, or the task was "
            f"completed without needing changes. The next run should clarify "
            f"the task intent if it remains unresolved."
        ),
        evidence=[
            f"Meaningful transcript events: {meaningful_count}",
            f"Changed files: {len(changed_files)}",
            f"Task prompt: {(run.task_prompt or '')[:150] if run else 'unknown'}",
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _any_event_kind(events: list, kind: str) -> bool:
    return any(e.kind == kind for e in events)


# ---------------------------------------------------------------------------
# Detector registry
# ---------------------------------------------------------------------------


_DETECTORS: list = [
    _detect_edits_without_tests,
    _detect_stuck_on_file,
    _detect_read_edit_divergence,
    _detect_command_failure_hidden,
    _detect_zero_meaningful_activity,
]
