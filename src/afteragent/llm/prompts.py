from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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
    # Prevent overlap: tail starts at max(head_lines, len(lines) - tail_lines)
    tail_start = max(head_lines, len(lines) - tail_lines)
    tail = "\n".join(lines[tail_start:]) if len(lines) > head_lines else ""
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


# Hard ceiling on prompt size. Spec budget is ~8-15k typical, ~25k absolute max.
MAX_INPUT_TOKENS = 25_000


@dataclass(slots=True)
class MergedFinding:
    """A finding after LLM merge against rule-based findings. Used by the
    interventions prompt builder and persisted by the enhancer.
    """
    code: str
    title: str
    severity: str
    summary: str
    evidence: list[str]
    source: str  # "rule" | "llm"


def estimate_tokens(text: str) -> int:
    """Rough heuristic: ~4 characters per token.

    Good enough for budget enforcement; the actual tokenizer varies by model
    and we don't want to import model-specific tokenizers just for this.
    """
    return len(text) // 4


_DIAGNOSIS_SYSTEM_PROMPT = """You are a failure-pattern diagnostician for AI coding agent runs. You will be given a single run's full context (the agent's actions, the code changes, the test/CI results, and the pull request review state) plus a list of rule-based findings from a cheap regex detector. Your job:

1. For each rule-based finding, decide whether it applies to THIS specific run. Mark it confirmed_rule (yes, keep it with personalized summary/evidence that cites specifics from this run), rejected_rule (no, this is a false positive — explain why in the summary), or ignore it entirely if you have no opinion.

2. Identify novel failure patterns the rules missed. Common patterns worth naming: agent edited files unrelated to the failure, agent ran tests targeting the wrong file, agent ignored the most specific error message, agent got stuck in a read-edit loop on one file, agent's plan and actions diverged, agent bypassed a review comment about the exact file it edited.

3. Be specific. Cite file paths, test names, error messages, and tool call sequences from the context. Generic findings like "the agent was confused" are not useful.

4. Limit severity=high to findings that, if unaddressed, will cause the next run to fail the same way. Use medium for concerns that probably matter and low for observations worth noting.

5. Return at most 12 findings total. Quality over quantity. Prefer one specific novel finding over three vague confirmed_rule entries.

Output via the `report_findings` tool."""


_INTERVENTIONS_SYSTEM_PROMPT = """You are authoring corrective instructions that will be added to an AI coding agent's instruction file (AGENTS.md / CLAUDE.md), injected into its next task prompt, or written as a runner policy. You will be given a single run's context plus the merged set of confirmed findings from a prior diagnosis pass.

For each relevant finding, author one or more interventions in the existing vocabulary:
- instruction_patch — durable rule to add to the agent's repo instructions
- prompt_patch — text to prepend to the next task prompt
- context_injection_rule — runner-side rule about what context to feed the agent
- runtime_guardrail — policy the runner enforces at tool-call time
- tool_policy_rule — rule about which tools to allow/deny/prefer

Rules:
1. Name specific files, tests, and review comments from the context. Generic advice ("read the failing test first") is less useful than specific advice ("before editing, read tests/test_foo.py::test_add and reconcile with the unresolved review comment at src/arithmetic.py:42").

2. Interventions should be preventative — the next run reading them should naturally avoid the failure.

3. Prefer instruction_patch for durable patterns ("our repo uses X"), prompt_patch for one-off task framing, and runtime_guardrail only when the runner has real control over tool invocations.

4. Each intervention's content field is a plain-text block the agent will read verbatim. Write it in second person, imperative voice.

5. Return at most 10 interventions total. Set related_finding_codes to the finding codes each intervention addresses.

Output via the `author_interventions` tool."""


def build_diagnosis_prompt(context: DiagnosisContext) -> tuple[str, str]:
    """Build (system, user) strings for the findings call."""
    def rebuild_diagnosis(ctx: DiagnosisContext) -> str:
        return _build_base_context_block(ctx, include_findings_header="Rule-based findings")

    user = rebuild_diagnosis(context)
    user = _enforce_token_budget(user, context, rebuild_diagnosis)
    return (_DIAGNOSIS_SYSTEM_PROMPT, user)


def build_interventions_prompt(
    context: DiagnosisContext,
    merged_findings: list[MergedFinding],
) -> tuple[str, str]:
    """Build (system, user) strings for the interventions call."""
    def rebuild_interventions(ctx: DiagnosisContext) -> str:
        base = _build_base_context_block(ctx, include_findings_header=None)
        if merged_findings:
            findings_section = "## Confirmed findings to address\n\n" + json.dumps(
                [
                    {
                        "code": f.code,
                        "title": f.title,
                        "severity": f.severity,
                        "summary": f.summary,
                        "evidence": f.evidence,
                        "source": f.source,
                    }
                    for f in merged_findings
                ],
                indent=2,
            )
        else:
            findings_section = "## Confirmed findings to address\n\n(none)"
        return f"{findings_section}\n\n{base}"

    user = rebuild_interventions(context)
    user = _enforce_token_budget(user, context, rebuild_interventions)
    return (_INTERVENTIONS_SYSTEM_PROMPT, user)


def _build_base_context_block(
    context: DiagnosisContext,
    include_findings_header: str | None,
) -> str:
    """The context sections shared between both prompts."""
    sections: list[str] = []

    sections.append(
        f"## Run metadata\n"
        f"id: {context.run.id}\n"
        f"command: {context.run.command}\n"
        f"status: {context.run.status} (exit code {context.run.exit_code})\n"
        f"duration_ms: {context.run.duration_ms}\n"
        f"cwd: {context.run.cwd}\n"
        f"summary: {context.run.summary or '(none)'}\n"
    )

    if include_findings_header is not None:
        if context.rule_findings:
            findings_json = json.dumps(
                [
                    {
                        "code": f.code,
                        "title": f.title,
                        "severity": f.severity,
                        "summary": f.summary,
                        "evidence": f.evidence,
                    }
                    for f in context.rule_findings
                ],
                indent=2,
            )
            sections.append(f"## {include_findings_header}\n\n{findings_json}")
        else:
            sections.append(f"## {include_findings_header}\n\n(none)")

    if context.transcript_events:
        events_json = json.dumps(
            [
                {
                    "sequence": e.sequence,
                    "kind": e.kind,
                    "tool_name": e.tool_name,
                    "target": e.target,
                    "inputs_summary": (e.inputs_summary or "")[:150],
                    "output_excerpt": (e.output_excerpt or "")[:200],
                    "status": e.status,
                    "source": e.source,
                }
                for e in context.transcript_events
            ],
            indent=2,
        )
        sections.append(
            f"## Transcript events ({len(context.transcript_events)} total)\n\n{events_json}"
        )
    else:
        sections.append("## Transcript events\n\n(none)")

    if context.diff_text.strip():
        sections.append(f"## Git diff\n\n```diff\n{context.diff_text}\n```")
    else:
        sections.append("## Git diff\n\n(empty)")

    if context.changed_files:
        sections.append(
            "## Changed files\n\n" + "\n".join(f"- {p}" for p in context.changed_files)
        )
    else:
        sections.append("## Changed files\n\n(none)")

    if context.stdout_head or context.stdout_tail:
        sections.append(
            f"## stdout (head)\n\n```\n{context.stdout_head}\n```\n\n"
            f"## stdout (tail)\n\n```\n{context.stdout_tail}\n```"
        )

    if context.stderr_head or context.stderr_tail:
        sections.append(
            f"## stderr (head)\n\n```\n{context.stderr_head}\n```\n\n"
            f"## stderr (tail)\n\n```\n{context.stderr_tail}\n```"
        )

    if context.github_summary is not None:
        gh = context.github_summary
        sections.append(
            f"## GitHub PR context\n"
            f"repo: {gh.repo}\n"
            f"pr_number: {gh.pr_number}\n"
            f"failing_checks: {json.dumps(gh.failing_checks, indent=2)}\n"
            f"unresolved_review_threads: {json.dumps(gh.unresolved_review_threads, indent=2)}\n"
            f"ci_log_excerpts: {json.dumps(gh.ci_log_excerpts, indent=2)}"
        )

    return "\n\n".join(sections)


def _enforce_token_budget(
    user: str,
    context: DiagnosisContext,
    rebuild_prompt: Callable[[DiagnosisContext], str],
) -> str:
    """Trim the user prompt to fit under MAX_INPUT_TOKENS.

    Strategy: if over budget, trim the transcript events section first
    (usually the biggest), then hard-clip the whole prompt as a fallback.
    The rebuild_prompt callback is used to re-render the exact same prompt
    shape against a trimmed DiagnosisContext.
    """
    if estimate_tokens(user) <= MAX_INPUT_TOKENS:
        return user

    if len(context.transcript_events) > 100:
        trimmed_events = context.transcript_events[:50] + context.transcript_events[-50:]
        trimmed_ctx = DiagnosisContext(
            run=context.run,
            rule_findings=context.rule_findings,
            transcript_events=trimmed_events,
            stdout_head=context.stdout_head,
            stdout_tail=context.stdout_tail,
            stderr_head=context.stderr_head,
            stderr_tail=context.stderr_tail,
            diff_text=context.diff_text,
            changed_files=context.changed_files,
            github_summary=context.github_summary,
        )
        user = rebuild_prompt(trimmed_ctx)
        if estimate_tokens(user) <= MAX_INPUT_TOKENS:
            return user + "\n\n[transcript events trimmed to first+last 50 of ~{} total]".format(
                len(context.transcript_events)
            )

    max_chars = MAX_INPUT_TOKENS * 4
    if len(user) > max_chars:
        user = user[:max_chars] + "\n\n[prompt truncated at character budget]"
    return user