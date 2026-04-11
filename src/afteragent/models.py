from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(slots=True)
class RunRecord:
    id: str
    command: str
    cwd: str
    status: str
    exit_code: int | None
    created_at: str
    finished_at: str | None
    duration_ms: int | None
    summary: str | None
    task_prompt: str | None  # NEW — nullable, added in sub-project 4


@dataclass(slots=True)
class EventRecord:
    id: int
    run_id: str
    event_type: str
    timestamp: str
    payload_json: str


@dataclass(slots=True)
class TranscriptEventRow:
    id: int
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


@dataclass(slots=True)
class PatternFinding:
    code: str
    title: str
    severity: str
    summary: str
    evidence: list[str]


@dataclass(slots=True)
class Intervention:
    type: str
    title: str
    target: str
    content: str
    scope: str = "pr"


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
