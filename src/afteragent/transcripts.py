from __future__ import annotations

import re
from dataclasses import dataclass

# Event kinds — tell downstream consumers what class of action occurred.
KIND_FILE_READ = "file_read"
KIND_FILE_EDIT = "file_edit"
KIND_BASH_COMMAND = "bash_command"
KIND_TEST_RUN = "test_run"
KIND_SEARCH = "search"
KIND_WEB_FETCH = "web_fetch"
KIND_TODO_UPDATE = "todo_update"
KIND_SUBAGENT_CALL = "subagent_call"
KIND_ASSISTANT_MESSAGE = "assistant_message"
KIND_USER_MESSAGE = "user_message"
KIND_HOOK_EVENT = "hook_event"
KIND_PARSE_ERROR = "parse_error"
KIND_UNKNOWN = "unknown"

# Source identifiers — tells downstream consumers the fidelity of the event.
SOURCE_CLAUDE_CODE_JSONL = "claude_code_jsonl"
SOURCE_CODEX_STDOUT = "codex_stdout"
SOURCE_STDOUT_HEURISTIC = "stdout_heuristic"


@dataclass(slots=True)
class TranscriptEvent:
    # The run this event belongs to (foreign key into runs table).
    run_id: str
    # 0-indexed monotonic order within the run. Guaranteed to exist even when
    # timestamps are not (e.g. the generic stdout parser has no clock).
    sequence: int
    # One of the KIND_* constants in this module.
    kind: str
    # Canonical tool name (e.g. "Read", "Edit", "Bash"). None for non-tool events.
    tool_name: str | None
    # File path / command / URL. Unified so downstream queries stay simple.
    target: str | None
    # Parser source identifier — one of the SOURCE_* constants in this module.
    # Tells downstream consumers how much to trust the event.
    source: str
    # Back-pointer into the raw artifact (e.g. "line:42") or None.
    raw_ref: str | None
    # Tool inputs / command args, truncated to INPUTS_SUMMARY_MAX chars with "…".
    inputs_summary: str = ""
    # Tool output / assistant text, truncated to OUTPUT_EXCERPT_MAX chars with "…".
    output_excerpt: str = ""
    # Tri-state: "success" | "error" | "unknown". "unknown" is explicit because
    # the generic parser often can't tell success from failure.
    status: str = "unknown"
    # ISO-8601 matching models.now_utc() format, or "" if unknown.
    timestamp: str = ""


INPUTS_SUMMARY_MAX = 200
OUTPUT_EXCERPT_MAX = 500
_ELLIPSIS = "…"


def truncate(text: str, max_len: int) -> str:
    """Return text unchanged if short enough; otherwise clip and append an ellipsis.

    The returned string is guaranteed to be at most max_len characters.
    A single-character ellipsis is used so the visible length matches max_len.
    """
    if len(text) <= max_len:
        return text
    if max_len <= 0:
        return ""
    return text[: max_len - 1] + _ELLIPSIS


def make_parse_error(
    run_id: str,
    sequence: int,
    source: str,
    message: str,
    raw_ref: str | None,
) -> TranscriptEvent:
    """Construct a parse_error event. The parser's message goes in output_excerpt."""
    return TranscriptEvent(
        run_id=run_id,
        sequence=sequence,
        kind=KIND_PARSE_ERROR,
        tool_name=None,
        target=None,
        inputs_summary="",
        output_excerpt=truncate(message, OUTPUT_EXCERPT_MAX),
        status="error",
        source=source,
        timestamp="",
        raw_ref=raw_ref,
    )


# Heuristic patterns. Low-fidelity on purpose — events are tagged with
# SOURCE_STDOUT_HEURISTIC so downstream consumers know to weight them.

_TEST_RUNNER_PATTERNS = (
    re.compile(r"test session starts", re.I),
    re.compile(r"(?:^|\s)pytest(?:\s|$)"),
    re.compile(r"(?:^|\s)jest(?:\s|$)"),
    re.compile(r"(?:^|\s)go test(?:\s|$)"),
    re.compile(r"(?:^|\s)npm (?:test|run test)(?:\s|$)"),
    re.compile(r"(?:^|\s)cargo test(?:\s|$)"),
    re.compile(r"(?:^|\s)mocha(?:\s|$)"),
    re.compile(r"(?:^|\s)vitest(?:\s|$)"),
    re.compile(r"(?:^|\s)rspec(?:\s|$)"),
)

_TEST_FAILURE_PATTERNS = (
    re.compile(r"\bfailed\b", re.I),
    re.compile(r"\bassertion\s*error\b", re.I),
    re.compile(r"\btraceback\b", re.I),
    re.compile(r"^\s*FAIL\s", re.I | re.MULTILINE),
)

_SUCCESS_PATTERNS = (
    re.compile(r"compiled successfully", re.I),
    re.compile(r"\bpassed\b.*\b0 failed\b", re.I),
    re.compile(r"\btests? passed\b", re.I),
    re.compile(r"\bok\b\s*$", re.I),
)


def parse_generic_stdout(
    run_id: str,
    stdout: str,
    stderr: str,
) -> list[TranscriptEvent]:
    """Best-effort heuristic parse of arbitrary CLI stdout/stderr.

    Produces low-fidelity events tagged SOURCE_STDOUT_HEURISTIC. Never raises —
    any parser failure becomes a parse_error event.
    """
    events: list[TranscriptEvent] = []
    sequence = 0
    combined = stdout + ("\n" + stderr if stderr else "")
    if not combined.strip():
        return events

    try:
        is_test_run = any(p.search(combined) for p in _TEST_RUNNER_PATTERNS)
        if is_test_run:
            has_failure = any(p.search(combined) for p in _TEST_FAILURE_PATTERNS)
            events.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=sequence,
                    kind=KIND_TEST_RUN,
                    tool_name=None,
                    target=None,
                    inputs_summary=truncate(
                        _first_meaningful_line(combined), INPUTS_SUMMARY_MAX
                    ),
                    output_excerpt=truncate(
                        _last_meaningful_lines(combined, 10), OUTPUT_EXCERPT_MAX
                    ),
                    status="error" if has_failure else "unknown",
                    source=SOURCE_STDOUT_HEURISTIC,
                    timestamp="",
                    raw_ref=None,
                )
            )
            sequence += 1

        has_success = any(p.search(combined) for p in _SUCCESS_PATTERNS)
        if has_success:
            events.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=sequence,
                    kind=KIND_UNKNOWN,
                    tool_name=None,
                    target=None,
                    inputs_summary="",
                    output_excerpt=truncate(
                        _last_meaningful_lines(combined, 5), OUTPUT_EXCERPT_MAX
                    ),
                    status="success",
                    source=SOURCE_STDOUT_HEURISTIC,
                    timestamp="",
                    raw_ref=None,
                )
            )
            sequence += 1
    except Exception as exc:  # never allow ingestion to fail the run
        events.append(
            make_parse_error(
                run_id=run_id,
                sequence=sequence,
                source=SOURCE_STDOUT_HEURISTIC,
                message=f"generic parser raised: {exc}",
                raw_ref=None,
            )
        )

    return events


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _last_meaningful_lines(text: str, n: int) -> str:
    meaningful = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(meaningful[-n:])
