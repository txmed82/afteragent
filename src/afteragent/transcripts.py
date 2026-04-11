from __future__ import annotations

from dataclasses import dataclass

# Event kinds — see spec section "Normalized event schema".
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
    run_id: str
    sequence: int
    kind: str
    tool_name: str | None
    target: str | None
    inputs_summary: str
    output_excerpt: str
    status: str
    source: str
    timestamp: str
    raw_ref: str | None
