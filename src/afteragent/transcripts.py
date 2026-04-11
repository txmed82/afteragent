from __future__ import annotations

import json
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


# Tool names that map to specific event kinds.
_CLAUDE_TOOL_KIND_MAP = {
    "Read": KIND_FILE_READ,
    "Edit": KIND_FILE_EDIT,
    "Write": KIND_FILE_EDIT,
    "NotebookEdit": KIND_FILE_EDIT,
    "Bash": KIND_BASH_COMMAND,
    "Grep": KIND_SEARCH,
    "Glob": KIND_SEARCH,
    "WebFetch": KIND_WEB_FETCH,
    "WebSearch": KIND_WEB_FETCH,
    "TodoWrite": KIND_TODO_UPDATE,
    "TaskCreate": KIND_TODO_UPDATE,
    "TaskUpdate": KIND_TODO_UPDATE,
    "Task": KIND_SUBAGENT_CALL,
    "Agent": KIND_SUBAGENT_CALL,
}

# Bash command prefixes that indicate a test run.
_TEST_COMMAND_PATTERNS = (
    re.compile(r"^\s*pytest\b"),
    re.compile(r"^\s*python\s+-m\s+pytest\b"),
    re.compile(r"^\s*jest\b"),
    re.compile(r"^\s*npm\s+(?:run\s+)?test\b"),
    re.compile(r"^\s*yarn\s+test\b"),
    re.compile(r"^\s*go\s+test\b"),
    re.compile(r"^\s*cargo\s+test\b"),
    re.compile(r"^\s*mocha\b"),
    re.compile(r"^\s*vitest\b"),
    re.compile(r"^\s*rspec\b"),
    re.compile(r"^\s*bundle\s+exec\s+rspec\b"),
)


def parse_claude_code_jsonl(run_id: str, jsonl_text: str) -> list[TranscriptEvent]:
    """Parse a Claude Code session JSONL into normalized transcript events.

    Never raises. Malformed lines become parse_error events; the rest still parse.
    Tool results from user-role messages are attached to their matching tool_use
    events by tool_use_id, not emitted as standalone user_message events.
    """
    events: list[TranscriptEvent] = []
    sequence = 0
    # Map from tool_use_id to the already-created TranscriptEvent so tool_result
    # blocks (which arrive in a later record) can update the right event in place.
    tool_events_by_id: dict[str, TranscriptEvent] = {}

    for line_num, line in enumerate(jsonl_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            events.append(
                make_parse_error(
                    run_id=run_id,
                    sequence=sequence,
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    message=f"JSONL line {line_num} invalid: {exc}",
                    raw_ref=f"line:{line_num}",
                )
            )
            sequence += 1
            continue

        try:
            new_events = _events_from_jsonl_record(
                run_id=run_id,
                record=record,
                line_num=line_num,
                next_sequence=sequence,
                tool_events_by_id=tool_events_by_id,
            )
        except Exception as exc:
            events.append(
                make_parse_error(
                    run_id=run_id,
                    sequence=sequence,
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    message=f"record parse raised on line {line_num}: {exc}",
                    raw_ref=f"line:{line_num}",
                )
            )
            sequence += 1
            continue

        for event in new_events:
            events.append(event)
            sequence += 1

    return events


def _events_from_jsonl_record(
    run_id: str,
    record: dict,
    line_num: int,
    next_sequence: int,
    tool_events_by_id: dict[str, TranscriptEvent],
) -> list[TranscriptEvent]:
    """Translate a single JSONL record into zero or more TranscriptEvents.

    tool_events_by_id is a running map (maintained by the caller) from
    tool_use_id to the already-emitted event. tool_use blocks register into it;
    tool_result blocks look up and mutate the registered event directly, which
    is why they do NOT produce a new event and do NOT advance the sequence.
    """
    out: list[TranscriptEvent] = []
    seq = next_sequence
    raw_ref = f"line:{line_num}"
    timestamp = record.get("timestamp", "") or ""

    # Hook events.
    attachment = record.get("attachment") or {}
    if attachment.get("type", "").startswith("hook_") or attachment.get("hookEvent"):
        out.append(
            TranscriptEvent(
                run_id=run_id,
                sequence=seq,
                kind=KIND_HOOK_EVENT,
                tool_name=None,
                target=attachment.get("hookEvent") or attachment.get("hookName"),
                inputs_summary="",
                output_excerpt=truncate(
                    str(attachment.get("content") or attachment.get("stdout") or ""),
                    OUTPUT_EXCERPT_MAX,
                ),
                status="unknown",
                source=SOURCE_CLAUDE_CODE_JSONL,
                timestamp=timestamp,
                raw_ref=raw_ref,
            )
        )
        seq += 1
        return out

    message = record.get("message")
    if not isinstance(message, dict):
        return out

    role = message.get("role")
    content = message.get("content")
    if not isinstance(content, list):
        text = str(content) if content is not None else ""
        kind = KIND_ASSISTANT_MESSAGE if role == "assistant" else KIND_USER_MESSAGE
        if text:
            out.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=seq,
                    kind=kind,
                    tool_name=None,
                    target=None,
                    inputs_summary="",
                    output_excerpt=truncate(text, OUTPUT_EXCERPT_MAX),
                    status="unknown",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp=timestamp,
                    raw_ref=raw_ref,
                )
            )
            seq += 1
        return out

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "") or ""
            if not text.strip():
                continue
            kind = KIND_ASSISTANT_MESSAGE if role == "assistant" else KIND_USER_MESSAGE
            out.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=seq,
                    kind=kind,
                    tool_name=None,
                    target=None,
                    inputs_summary="",
                    output_excerpt=truncate(text, OUTPUT_EXCERPT_MAX),
                    status="unknown",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp=timestamp,
                    raw_ref=raw_ref,
                )
            )
            seq += 1

        elif btype == "tool_use":
            tool_name = block.get("name") or "unknown"
            tool_input = block.get("input") or {}
            tool_use_id = block.get("id") or ""
            kind = _classify_tool(tool_name, tool_input)
            target = _extract_target(tool_name, tool_input)
            event = TranscriptEvent(
                run_id=run_id,
                sequence=seq,
                kind=kind,
                tool_name=tool_name,
                target=target,
                inputs_summary=truncate(
                    json.dumps(tool_input, sort_keys=True, default=str),
                    INPUTS_SUMMARY_MAX,
                ),
                output_excerpt="",
                status="unknown",
                source=SOURCE_CLAUDE_CODE_JSONL,
                timestamp=timestamp,
                raw_ref=raw_ref,
            )
            out.append(event)
            if tool_use_id:
                tool_events_by_id[tool_use_id] = event
            seq += 1

        elif btype == "tool_result":
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_text = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in result_content
                )
            else:
                result_text = str(result_content)
            is_error = bool(block.get("is_error"))
            tool_use_id = block.get("tool_use_id") or ""
            target_event = tool_events_by_id.get(tool_use_id)
            if target_event is not None:
                # Mutate the already-emitted tool_use event in place. No new
                # event is created and sequence does not advance — the result
                # is an annotation on the existing tool call.
                target_event.output_excerpt = truncate(result_text, OUTPUT_EXCERPT_MAX)
                target_event.status = "error" if is_error else "success"
            else:
                # Unmatched tool_result — emit as a standalone user_message so
                # the content isn't lost.
                out.append(
                    TranscriptEvent(
                        run_id=run_id,
                        sequence=seq,
                        kind=KIND_USER_MESSAGE,
                        tool_name=None,
                        target=None,
                        inputs_summary="",
                        output_excerpt=truncate(result_text, OUTPUT_EXCERPT_MAX),
                        status="error" if is_error else "unknown",
                        source=SOURCE_CLAUDE_CODE_JSONL,
                        timestamp=timestamp,
                        raw_ref=raw_ref,
                    )
                )
                seq += 1

    return out


def _classify_tool(tool_name: str, tool_input: dict) -> str:
    base = _CLAUDE_TOOL_KIND_MAP.get(tool_name, KIND_UNKNOWN)
    if base == KIND_BASH_COMMAND:
        command = str(tool_input.get("command", ""))
        if any(p.search(command) for p in _TEST_COMMAND_PATTERNS):
            return KIND_TEST_RUN
    return base


def _extract_target(tool_name: str, tool_input: dict) -> str | None:
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        return tool_input.get("file_path") or tool_input.get("notebook_path")
    if tool_name in ("Grep", "Glob"):
        return tool_input.get("pattern") or tool_input.get("path")
    if tool_name == "Bash":
        return str(tool_input.get("command", "")) or None
    if tool_name in ("WebFetch", "WebSearch"):
        return tool_input.get("url") or tool_input.get("query")
    if tool_name in ("Task", "Agent"):
        return tool_input.get("subagent_type") or tool_input.get("description")
    return None
