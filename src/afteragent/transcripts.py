from __future__ import annotations

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
