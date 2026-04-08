from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Intervention, PatternFinding
from .store import Store

FAILURE_FILE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|go|rb|java|kt|rs|c|cc|cpp|h|hpp|cs|swift|json|ya?ml|toml|rst|md))(?:(?:::|:|\()\d+)?(?![A-Za-z0-9_])"
)


def analyze_run(store: Store, run_id: str) -> tuple[list[PatternFinding], list[Intervention]]:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")

    context = load_run_context(store, run_id)
    related_contexts = load_related_contexts(store, run.id, run.cwd, run.created_at, context["gh_context"])

    findings: list[PatternFinding] = []

    ci_failure = active_ci_failures_present(context)
    if ci_failure:
        findings.append(
            PatternFinding(
                code="active_ci_failures_present",
                title="Active CI failures present",
                severity="high",
                summary="The PR currently has failing checks with captured CI evidence. Any repair loop should start by summarizing these failures before editing.",
                evidence=ci_failure,
            )
        )

    unresolved_review = unresolved_review_threads_present(context)
    if unresolved_review:
        findings.append(
            PatternFinding(
                code="unresolved_review_threads_present",
                title="Unresolved review threads present",
                severity="medium",
                summary="The PR already has unresolved review feedback. The next run should account for that feedback explicitly before making changes.",
                evidence=unresolved_review,
            )
        )

    if comments_ignored_after_they_existed(context):
        findings.append(
            PatternFinding(
                code="comments_ignored_after_they_existed",
                title="Comments ignored after they existed",
                severity="high",
                summary="Unresolved review comments already existed, but the current diff does not touch the commented files. The run likely ignored active PR feedback.",
                evidence=[
                    f"Unresolved review files: {', '.join(sorted(context['unresolved_comment_paths'])) or 'none'}",
                    f"Changed files: {', '.join(sorted(context['analysis_files'])) or 'none'}",
                ],
            )
        )

    if diff_misses_failing_files(context):
        findings.append(
            PatternFinding(
                code="low_diff_overlap_with_failing_files",
                title="Diff misses failing files",
                severity="high",
                summary="The patch does not overlap the files implicated by CI failures or unresolved review threads. That makes the fix path hard to trust.",
                evidence=[
                    f"Failing files: {', '.join(sorted(context['failure_files'])) or 'none'}",
                    f"Changed files: {', '.join(sorted(context['analysis_files'])) or 'none'}",
                    f"Overlap: {', '.join(sorted(context['analysis_files'] & context['failure_files'])) or 'none'}",
                ],
            )
        )

    repeated = repeated_failures(context, related_contexts)
    if repeated:
        findings.append(
            PatternFinding(
                code="same_failure_repeated_across_runs",
                title="Same failure repeated across runs",
                severity="high",
                summary="The current run matches the failure signature from an earlier run in the same PR context. Another edit-only retry is unlikely to help.",
                evidence=repeated,
            )
        )

    drift = broad_edit_drift(context, related_contexts)
    if drift:
        findings.append(
            PatternFinding(
                code="broad_edit_drift",
                title="Broad edit drift",
                severity="medium",
                summary="The changed file set has expanded or drifted away from the prior attempt. The repair loop is losing focus between runs.",
                evidence=drift,
            )
        )

    interventions = build_interventions(findings)
    store.replace_diagnosis(
        run_id,
        [
            {
                "run_id": run_id,
                "code": finding.code,
                "title": finding.title,
                "severity": finding.severity,
                "summary": finding.summary,
                "evidence_json": json.dumps(finding.evidence),
            }
            for finding in findings
        ],
        [
            {
                "run_id": run_id,
                "type": intervention.type,
                "title": intervention.title,
                "target": intervention.target,
                "content": intervention.content,
                "scope": intervention.scope,
            }
            for intervention in interventions
        ],
    )
    return findings, interventions


def build_interventions(findings: list[PatternFinding]) -> list[Intervention]:
    interventions: list[Intervention] = []
    codes = {finding.code for finding in findings}

    if "comments_ignored_after_they_existed" in codes:
        interventions.append(
            Intervention(
                type="instruction_patch",
                title="Require review and CI context before edits",
                target="repo_instructions",
                content=(
                    "Before editing code on an open PR, gather unresolved review comments "
                    "and current CI failures. Summarize both before making code changes."
                ),
                scope="pr",
            )
        )

    if "unresolved_review_threads_present" in codes:
        interventions.append(
            Intervention(
                type="context_injection_rule",
                title="Inject unresolved review threads into the next run",
                target="runner_context",
                content=(
                    "Before editing an open PR, inject unresolved review thread summaries and "
                    "their referenced files into the agent context."
                ),
                scope="pr",
            )
        )

    if "active_ci_failures_present" in codes:
        interventions.append(
            Intervention(
                type="instruction_patch",
                title="Require CI failure summary before edits",
                target="repo_instructions",
                content=(
                    "Before editing code on a PR, summarize all currently failing CI checks "
                    "and quote the key error lines before making changes."
                ),
                scope="pr",
            )
        )
        interventions.append(
            Intervention(
                type="prompt_patch",
                title="Summarize failing CI checks before editing",
                target="task_prompt",
                content=(
                    "Before writing code, list the currently failing CI checks, quote the key "
                    "error lines, and identify the likely files or workflows involved."
                ),
                scope="pr",
            )
        )

    if "low_diff_overlap_with_failing_files" in codes:
        interventions.append(
            Intervention(
                type="instruction_patch",
                title="Require edit-to-failure mapping",
                target="repo_instructions",
                content=(
                    "Before editing, list the files implicated by CI failures or review "
                    "threads and explain how each planned edit maps to that failure surface."
                ),
                scope="pr",
            )
        )
        interventions.append(
            Intervention(
                type="prompt_patch",
                title="Map the failure surface before editing",
                target="task_prompt",
                content=(
                    "Before writing code, list the files implicated by CI failures, review "
                    "threads, and stack traces. Explain how each planned edit maps to that set."
                ),
                scope="pr",
            )
        )

    if "same_failure_repeated_across_runs" in codes:
        interventions.append(
            Intervention(
                type="runtime_guardrail",
                title="Switch to diagnosis mode after repeated failures",
                target="runner_policy",
                content=(
                    "If the same test or assertion signature fails twice, stop editing and "
                    "enter diagnosis mode instead of retrying the same patch strategy."
                ),
                scope="pr",
            )
        )

    if "broad_edit_drift" in codes:
        interventions.append(
            Intervention(
                type="tool_policy_rule",
                title="Warn on drifting patch scope",
                target="runner_policy",
                content=(
                    "Warn when the changed file set expands materially or loses overlap with the "
                    "previous attempt, and require file-by-file justification before continuing."
                ),
                scope="pr",
            )
        )

    return interventions


def count_changed_files(diff_text: str) -> int:
    return len(extract_changed_files(diff_text))


def load_run_context(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    artifact_dir = store.run_artifact_dir(run_id)
    stdout = read_text(artifact_dir / "stdout.log")
    stderr = read_text(artifact_dir / "stderr.log")
    after_diff = read_text(artifact_dir / "git_diff_after.patch")
    gh_context = read_json(artifact_dir / "github_context.json")
    changed_files = extract_changed_files(after_diff)
    pr_changed_files = set(gh_context.get("pr_changed_files", []))
    analysis_files = changed_files or pr_changed_files
    failure_files = extract_failure_files(stdout, stderr, gh_context)
    failure_signatures = extract_failure_signatures(stdout, stderr, gh_context)
    unresolved_paths = unresolved_review_paths(gh_context, run.created_at)
    return {
        "run": run,
        "stdout": stdout,
        "stderr": stderr,
        "gh_context": gh_context,
        "changed_files": changed_files,
        "pr_changed_files": pr_changed_files,
        "analysis_files": analysis_files,
        "failure_files": failure_files,
        "failure_signatures": failure_signatures,
        "unresolved_comment_paths": unresolved_paths,
    }


def load_related_contexts(
    store: Store,
    run_id: str,
    cwd: str,
    created_at: str,
    gh_context: dict,
) -> list[dict]:
    related: list[dict] = []
    current_repo = gh_context.get("repo")
    current_pr_number = gh_context.get("pr_number")
    for previous_run in store.list_previous_runs(cwd, created_at, limit=10):
        if previous_run.id == run_id:
            continue
        previous_context = load_run_context(store, previous_run.id)
        previous_gh = previous_context["gh_context"]
        if current_repo and current_pr_number:
            if (
                previous_gh.get("repo") != current_repo
                or previous_gh.get("pr_number") != current_pr_number
            ):
                continue
        related.append(previous_context)
    return related


def extract_changed_files(diff_text: str) -> set[str]:
    files = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.add(parts[2].removeprefix("a/"))
    return files


def extract_failure_signatures(stdout: str, stderr: str, gh_context: dict) -> set[str]:
    signatures = {
        normalize_failure_line(line)
        for line in extract_failure_lines(stdout, stderr, gh_context)
        if normalize_failure_line(line)
    }
    for check in gh_context.get("checks", []):
        bucket = (check.get("bucket") or "").lower()
        state = (check.get("state") or "").lower()
        if bucket == "fail" or state in {"fail", "failure", "error", "timed_out"}:
            signatures.add(f"check:{(check.get('name') or '').strip().lower()}")
    return signatures


def extract_failure_lines(stdout: str, stderr: str, gh_context: dict) -> list[str]:
    lines: list[str] = []
    for source in (stderr, stdout):
        for line in source.splitlines():
            lowered = line.lower()
            if any(token in lowered for token in ["assert", "failed", "error", "traceback"]):
                lines.append(line.strip())
            if len(lines) >= 6:
                return lines
    for ci_run in gh_context.get("ci_runs", []):
        for line in ci_run.get("failed_log_excerpt", []):
            lines.append(line.strip())
            if len(lines) >= 6:
                return lines
    return lines


def extract_failure_files(stdout: str, stderr: str, gh_context: dict) -> set[str]:
    files = set(unresolved_review_paths(gh_context, None))
    known_paths = files | set(gh_context.get("pr_changed_files", []))
    sources = [stdout, stderr]
    for ci_run in gh_context.get("ci_runs", []):
        excerpt = "\n".join(ci_run.get("failed_log_excerpt", []))
        if excerpt:
            sources.append(excerpt)
    for source in sources:
        for line in source.splitlines():
            for candidate in extract_file_candidates_from_line(line, known_paths):
                files.add(candidate)
    return files


def unresolved_review_paths(gh_context: dict, run_created_at: str | None) -> set[str]:
    paths = set()
    for thread in gh_context.get("review_threads", []):
        if thread.get("is_resolved"):
            continue
        timestamp = thread.get("latest_comment_at")
        if run_created_at and timestamp and timestamp > run_created_at:
            continue
        path = thread.get("path")
        if path:
            paths.add(path)
    return paths


def comments_ignored_after_they_existed(context: dict) -> bool:
    unresolved_paths = context["unresolved_comment_paths"]
    changed_files = context["analysis_files"]
    return bool(unresolved_paths) and bool(changed_files) and not bool(unresolved_paths & changed_files)


def diff_misses_failing_files(context: dict) -> bool:
    failing_files = context["failure_files"]
    changed_files = context["analysis_files"]
    if not failing_files or not changed_files:
        return False
    overlap = failing_files & changed_files
    return not overlap


def repeated_failures(context: dict, related_contexts: list[dict]) -> list[str]:
    current_signatures = context["failure_signatures"]
    if not current_signatures or not context["run"].exit_code:
        return []
    for previous in related_contexts:
        if not previous["run"].exit_code:
            continue
        overlap = sorted(current_signatures & previous["failure_signatures"])
        if overlap:
            return [
                f"Current failure signatures: {', '.join(sorted(current_signatures))}",
                f"Previous run: {previous['run'].id}",
                f"Shared signatures: {', '.join(overlap)}",
            ]
    return []


def broad_edit_drift(context: dict, related_contexts: list[dict]) -> list[str]:
    current_files = context["analysis_files"]
    if len(current_files) < 6:
        return []
    for previous in related_contexts:
        previous_files = previous["analysis_files"]
        if not previous_files:
            continue
        overlap = current_files & previous_files
        new_files = current_files - previous_files
        overlap_ratio = len(overlap) / max(len(current_files), 1)
        if len(new_files) >= 4 and overlap_ratio <= 0.35:
            return [
                f"Previous run: {previous['run'].id}",
                f"Previous changed files: {len(previous_files)}",
                f"Current changed files: {len(current_files)}",
                f"Shared files: {len(overlap)}",
                f"New files introduced: {', '.join(sorted(new_files))}",
            ]
    return []


def active_ci_failures_present(context: dict) -> list[str]:
    gh_context = context["gh_context"]
    failed_checks = [
        check for check in gh_context.get("checks", []) if (check.get("bucket") or "").lower() == "fail"
    ]
    if not failed_checks:
        return []
    evidence = [
        f"Failing checks: {', '.join(check.get('name') or 'unknown' for check in failed_checks)}",
        f"Changed files in PR: {', '.join(sorted(context['analysis_files'])) or 'none'}",
    ]
    for line in extract_failure_lines(context["stdout"], context["stderr"], gh_context)[:3]:
        evidence.append(line)
    return evidence


def unresolved_review_threads_present(context: dict) -> list[str]:
    gh_context = context["gh_context"]
    unresolved_count = gh_context.get("review_summary", {}).get("unresolved_thread_count", 0)
    if not unresolved_count:
        return []
    evidence = [
        f"Unresolved review thread count: {unresolved_count}",
        f"Files referenced by unresolved threads: {', '.join(sorted(context['unresolved_comment_paths'])) or 'none'}",
    ]
    for thread in gh_context.get("review_threads", []):
        if thread.get("is_resolved"):
            continue
        path = thread.get("path") or "unknown path"
        line = thread.get("line") or "?"
        evidence.append(f"Thread on {path}:{line}")
        if len(evidence) >= 5:
            break
    return evidence


def normalize_failure_line(line: str) -> str:
    normalized = re.sub(r"\s+", " ", line.strip().lower())
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    return normalized[:180]


def extract_file_candidates_from_line(line: str, known_paths: set[str]) -> set[str]:
    normalized_line = line.replace("\\.", ".")
    candidates: set[str] = set()
    for match in FAILURE_FILE_PATTERN.findall(normalized_line):
        candidate = sanitize_failure_candidate(match)
        if not candidate:
            continue
        if candidate in known_paths:
            candidates.add(candidate)
            continue
        if is_high_confidence_failure_file(candidate, normalized_line):
            candidates.add(candidate)
    return candidates


def sanitize_failure_candidate(candidate: str) -> str | None:
    value = candidate.strip(" '\"`()[]{}<>:,")
    value = value.replace("\\", "")
    if not value:
        return None
    if value.startswith(("http://", "https://", "//", ".")):
        return None
    if "..." in value or ".." in value:
        return None
    if "/" not in value and value.count(".") == 1 and value.endswith((".js", ".c", ".h")):
        return None
    return value


def is_high_confidence_failure_file(candidate: str, line: str) -> bool:
    lowered = line.lower()
    if f"{candidate}:" in line or f"{candidate}(" in line or f'"{candidate}"' in line:
        return True
    if any(token in lowered for token in ["failed to parse", "assertionerror", "traceback", "file ", "tests/"]):
        return True
    if "/" in candidate and candidate.startswith(("tests/", "src/", ".github/")):
        return True
    if candidate in {"pyproject.toml", "package.json", "package-lock.json", "poetry.lock"}:
        return True
    return False


def read_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())
