from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import PatternFinding, RunRecord, TranscriptEventRow
from ..store import Store

STDOUT_HEAD_LINES = 50
STDOUT_TAIL_LINES = 50
STDOUT_HEAD_CHAR_CAP = 5000
STDOUT_TAIL_CHAR_CAP = 5000
STDERR_HEAD_LINES = 30
STDERR_TAIL_LINES = 30
STDERR_HEAD_CHAR_CAP = 3000
STDERR_TAIL_CHAR_CAP = 3000
DIFF_CHAR_CAP = 20_000


@dataclass(slots=True)
class GithubSummary:
    repo: str | None
    pr_number: int | None
    failing_checks: list[dict]
    unresolved_review_threads: list[dict]
    ci_log_excerpts: list[str]


@dataclass(slots=True)
class RelatedRunSummary:
    run_id: str
    status: str
    exit_code: int | None
    changed_files: list[str]
    rule_finding_codes: list[str]


@dataclass(slots=True)
class DiagnosisContext:
    run: RunRecord
    rule_findings: list[PatternFinding]
    transcript_events: list[TranscriptEventRow]
    stdout_head: str
    stdout_tail: str
    stderr_head: str
    stderr_tail: str
    diff_text: str
    changed_files: list[str]
    github_summary: GithubSummary | None
    related_runs: list[RelatedRunSummary] = field(default_factory=list)


def load_diagnosis_context(store: Store, run_id: str) -> DiagnosisContext:
    """Assemble all the per-run signals the LLM prompt needs."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")

    artifact_dir = store.run_artifact_dir(run_id)
    stdout = _read_text(artifact_dir / "stdout.log")
    stderr = _read_text(artifact_dir / "stderr.log")
    diff_after = _read_text(artifact_dir / "git_diff_after.patch")

    stdout_head, stdout_tail = _head_and_tail(
        stdout, STDOUT_HEAD_LINES, STDOUT_TAIL_LINES,
        STDOUT_HEAD_CHAR_CAP, STDOUT_TAIL_CHAR_CAP,
    )
    stderr_head, stderr_tail = _head_and_tail(
        stderr, STDERR_HEAD_LINES, STDERR_TAIL_LINES,
        STDERR_HEAD_CHAR_CAP, STDERR_TAIL_CHAR_CAP,
    )
    diff_text = _cap_diff(diff_after)
    changed_files = _extract_changed_files(diff_after)

    rule_findings = _load_rule_findings(store, run_id)
    transcript_events = store.get_transcript_events(run_id)
    github_summary = _load_github_summary(artifact_dir)

    return DiagnosisContext(
        run=run,
        rule_findings=rule_findings,
        transcript_events=transcript_events,
        stdout_head=stdout_head,
        stdout_tail=stdout_tail,
        stderr_head=stderr_head,
        stderr_tail=stderr_tail,
        diff_text=diff_text,
        changed_files=changed_files,
        github_summary=github_summary,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _head_and_tail(
    text: str,
    head_lines: int,
    tail_lines: int,
    head_char_cap: int,
    tail_char_cap: int,
) -> tuple[str, str]:
    lines = text.splitlines()
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-(tail_lines + 1):]) if len(lines) > head_lines else ""
    if len(head) > head_char_cap:
        head = head[: head_char_cap - 1] + "\u2026"
    if len(tail) > tail_char_cap:
        tail = tail[: tail_char_cap - 1] + "\u2026"
    return head, tail


def _cap_diff(diff: str) -> str:
    if len(diff) <= DIFF_CHAR_CAP:
        return diff
    head = diff[:DIFF_CHAR_CAP]
    return f"{head}\n\n[diff truncated after {DIFF_CHAR_CAP} chars]"


_DIFF_GIT_LINE = re.compile(r"^diff --git a/(?P<path>[^ ]+) b/", re.MULTILINE)


def _extract_changed_files(diff: str) -> list[str]:
    return sorted(set(m.group("path") for m in _DIFF_GIT_LINE.finditer(diff)))


def _load_rule_findings(store: Store, run_id: str) -> list[PatternFinding]:
    rows = store.get_diagnoses(run_id)
    findings: list[PatternFinding] = []
    for row in rows:
        if row["source"] != "rule":
            continue
        evidence = []
        try:
            evidence = json.loads(row["evidence_json"])
            if not isinstance(evidence, list):
                evidence = []
        except (TypeError, ValueError):
            evidence = []
        findings.append(
            PatternFinding(
                code=row["code"],
                title=row["title"],
                severity=row["severity"],
                summary=row["summary"],
                evidence=[str(e) for e in evidence],
            )
        )
    return findings


def _load_github_summary(artifact_dir: Path) -> GithubSummary | None:
    gh_path = artifact_dir / "github_context.json"
    if not gh_path.exists():
        return None
    try:
        data = json.loads(gh_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return GithubSummary(
        repo=data.get("repo"),
        pr_number=data.get("pr_number"),
        failing_checks=[
            c for c in data.get("checks", []) if (c.get("bucket") or "").lower() == "fail"
        ],
        unresolved_review_threads=[
            t for t in data.get("review_threads", []) if not t.get("is_resolved")
        ],
        ci_log_excerpts=[
            line
            for ci_run in data.get("ci_runs", [])
            for line in (ci_run.get("failed_log_excerpt") or [])[:5]
        ],
    )
