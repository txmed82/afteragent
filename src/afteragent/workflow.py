from __future__ import annotations

import difflib
import json
import shlex
import uuid
from dataclasses import asdict
from pathlib import Path

from .adapters import (
    ADAPTERS,
    KNOWN_INSTRUCTION_FILES,
    RunnerAdapter,
    ShellAdapter,
    get_runner_adapter,
    select_runner_adapter,
)
from .capture import run_command, validate_github_pr
from .diagnostics import analyze_run, load_related_contexts, load_run_context
from .models import now_utc
from .store import Store

AFTERACTION_START = "<!-- AFTERACTION INTERVENTIONS START -->"
AFTERACTION_END = "<!-- AFTERACTION INTERVENTIONS END -->"
REPO_INSTRUCTION_TARGET = "repo_instructions"
DEFAULT_INSTRUCTION_FILE = "AGENTS.md"


def export_interventions(
    store: Store,
    run_id: str,
    base_dir: Path,
    output_dir: Path | None = None,
    adapter: RunnerAdapter | None = None,
) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")

    findings, interventions = analyze_run(store, run_id)
    context = load_run_context(store, run_id)
    related = load_related_contexts(store, run.id, run.cwd, run.created_at, context["gh_context"])
    set_id = uuid.uuid4().hex[:10]
    version = store.next_intervention_version(run_id)

    destination = output_dir or intervention_output_dir(store.paths.exports_dir, run_id, version, set_id)
    destination.mkdir(parents=True, exist_ok=True)

    prompt_text = build_prompt_export(findings, interventions)
    context_text = build_context_export(context, related)
    runner_policy = build_runner_policy(interventions)
    active_adapter = adapter or ShellAdapter()
    instruction_targets = resolve_instruction_targets(base_dir, interventions, active_adapter)
    instruction_patches = build_instruction_patches(base_dir, run_id, interventions, instruction_targets, context)
    replay_context = build_replay_context(run_id, run.command, context, related, interventions)

    prompt_path = destination / "task_prompt.txt"
    context_path = destination / "runner_context.txt"
    policy_path = destination / "runner_policy.json"
    replay_path = destination / "replay_context.json"
    manifest_path = destination / "interventions.json"
    prompt_path.write_text(prompt_text)
    context_path.write_text(context_text)
    policy_path.write_text(json.dumps(runner_policy, indent=2))
    replay_path.write_text(json.dumps(replay_context, indent=2))

    manifest = {
        "intervention_set_id": set_id,
        "version": version,
        "run_id": run_id,
        "generated_at": now_utc(),
        "run": {
            "command": run.command,
            "status": run.status,
            "summary": run.summary,
        },
        "findings": [asdict(finding) for finding in findings],
        "interventions": [asdict(intervention) for intervention in interventions],
        "context": {
            "repo": context["gh_context"].get("repo"),
            "pr_number": context["gh_context"].get("pr_number"),
        },
        "instruction_targets": [path.name for path in instruction_targets],
        "runner_adapter": active_adapter.name,
        "exports": {
            "task_prompt": str(prompt_path),
            "runner_context": str(context_path),
            "runner_policy": str(policy_path),
            "replay_context": str(replay_path),
        },
    }

    if instruction_patches:
        patch_paths: dict[str, str] = {}
        for target_name, patch_text in instruction_patches.items():
            patch_path = destination / f"{target_name}.patch"
            patch_path.write_text(patch_text)
            patch_paths[target_name] = str(patch_path)
        manifest["exports"]["instruction_patches"] = patch_paths

    manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest["manifest_path"] = str(manifest_path)
    store.save_intervention_set(
        set_id=set_id,
        source_run_id=run_id,
        version=version,
        kind="export",
        created_at=manifest["generated_at"],
        output_dir=str(destination),
        manifest=manifest,
    )
    store.add_event(
        run_id,
        "interventions.exported",
        manifest["generated_at"],
        {
            "intervention_set_id": set_id,
            "version": version,
            "output_dir": str(destination),
            "manifest_path": str(manifest_path),
        },
    )
    return manifest


def apply_interventions(
    store: Store,
    run_id: str,
    base_dir: Path,
    adapter: RunnerAdapter | None = None,
) -> dict:
    active_adapter = adapter or ShellAdapter()
    manifest = latest_export_manifest(store, run_id)
    if manifest is None or manifest.get("runner_adapter") != active_adapter.name:
        manifest = export_interventions(
            store,
            run_id,
            base_dir=base_dir,
            output_dir=None,
            adapter=active_adapter,
        )
    manifest["runner_adapter"] = active_adapter.name
    manifest["instruction_targets"] = [
        path.name for path in resolve_instruction_targets(base_dir, manifest["interventions"], active_adapter)
    ]
    set_id = manifest["intervention_set_id"]
    set_row = store.get_intervention_set(set_id)
    if not set_row:
        raise ValueError(f"Intervention set not found: {set_id}")
    applied_dir = intervention_output_dir(
        store.paths.applied_dir,
        run_id,
        manifest["version"],
        set_id,
    )
    applied_dir.mkdir(parents=True, exist_ok=True)
    copy_manifest_outputs(Path(set_row["output_dir"]), applied_dir)
    manifest["manifest_path"] = str(applied_dir / "interventions.json")
    manifest["exports"] = rewrite_manifest_paths(manifest["exports"], applied_dir)
    (applied_dir / "interventions.json").write_text(json.dumps(manifest, indent=2))
    applied_paths: list[str] = []

    repo_instruction_interventions = repo_instruction_entries(manifest["interventions"])
    applied_at = now_utc()
    store.mark_intervention_set_applied(set_id, applied_at)
    if repo_instruction_interventions:
        active_sets = active_instruction_sets(store)
        duplicate_ids = stale_set_ids(active_sets, current_set_id=set_id)
        store.supersede_intervention_sets(duplicate_ids, applied_at)
        active_sets = active_instruction_sets(store)
        target_paths = resolve_instruction_targets(
            base_dir,
            manifest["interventions"],
            active_adapter if adapter is not None else adapter_for_manifest(base_dir, manifest),
        )
        patch_paths: dict[str, str] = {}
        for target_path in target_paths:
            existing_text = target_path.read_text() if target_path.exists() else ""
            updated = render_instruction_file(existing_text, active_sets)
            target_path.write_text(updated)
            applied_paths.append(str(target_path))
            patch_text = build_instruction_patch_from_sets(existing_text, target_path, active_sets)
            if patch_text:
                patch_path = applied_dir / f"{target_path.name}.patch"
                patch_path.write_text(patch_text)
                patch_paths[target_path.name] = str(patch_path)
        if patch_paths:
            manifest["exports"]["instruction_patches"] = patch_paths

    store.add_event(
        run_id,
        "interventions.applied",
        applied_at,
        {
            "intervention_set_id": set_id,
            "version": manifest["version"],
            "output_dir": str(applied_dir),
            "applied_paths": applied_paths,
            "manifest_path": manifest["manifest_path"],
        },
    )
    manifest["applied_paths"] = applied_paths
    set_row = store.get_intervention_set(set_id)
    if set_row:
        updated_manifest = json.loads((applied_dir / "interventions.json").read_text())
        store.save_intervention_set(
            set_id=set_id,
            source_run_id=run_id,
            version=manifest["version"],
            kind="applied",
            created_at=manifest["generated_at"],
            output_dir=str(applied_dir),
            manifest=updated_manifest,
        )
        store.mark_intervention_set_applied(set_id, applied_at)
    return manifest


def replay_run(
    store: Store,
    source_run_id: str,
    cwd: Path,
    command: list[str] | None = None,
    summary: str | None = None,
    apply_interventions_first: bool = False,
    stream_output: bool = True,
    runner: str | None = None,
) -> dict:
    source_run = store.get_run(source_run_id)
    if not source_run:
        raise ValueError(f"Run not found: {source_run_id}")

    if command is None:
        command = shlex.split(source_run.command)
        if command and command[0] == "github-pr-validation":
            raise ValueError("Replay requires an explicit command for GitHub validation runs.")

    adapter = select_runner_adapter(cwd, command=command, source_command=source_run.command, preferred=runner)
    replay_root = store.paths.replays_dir / source_run_id / now_utc().replace(":", "-")
    exports = export_interventions(
        store,
        source_run_id,
        base_dir=cwd,
        output_dir=replay_root / "inputs",
        adapter=adapter,
    )
    if apply_interventions_first:
        apply_interventions(store, source_run_id, cwd, adapter=adapter)

    extra_env = {
        "AFTERACTION_SOURCE_RUN": source_run_id,
        "AFTERACTION_INTERVENTIONS_DIR": str(replay_root / "inputs"),
        "AFTERACTION_INTERVENTION_MANIFEST_PATH": exports["manifest_path"],
        "AFTERACTION_TASK_PROMPT_PATH": exports["exports"]["task_prompt"],
        "AFTERACTION_RUNNER_CONTEXT_PATH": exports["exports"]["runner_context"],
        "AFTERACTION_RUNNER_POLICY_PATH": exports["exports"]["runner_policy"],
        "AFTERACTION_REPLAY_CONTEXT_PATH": exports["exports"]["replay_context"],
    }
    launch_plan = adapter.launch(cwd, command, extra_env)

    result = run_command(
        store,
        launch_plan.command,
        cwd,
        summary=summary or f"Replay of run {source_run_id}",
        stream_output=stream_output,
        extra_env=launch_plan.env,
        adapter=adapter,
    )
    comparison = compare_runs(store, source_run_id, str(result["run_id"]))
    store.record_replay_run(
        source_run_id=source_run_id,
        replay_run_id=str(result["run_id"]),
        intervention_set_id=exports["intervention_set_id"],
        created_at=now_utc(),
        applied_before_replay=apply_interventions_first,
        comparison=comparison,
    )
    store.add_event(
        str(result["run_id"]),
        "replay.forked",
        now_utc(),
        {
            "source_run_id": source_run_id,
            "intervention_set_id": exports["intervention_set_id"],
            "runner_adapter": adapter.name,
            "input_dir": str(replay_root / "inputs"),
            "applied_before_replay": apply_interventions_first,
            "comparison": comparison,
        },
    )
    return result


def attempt_repair(
    store: Store,
    cwd: Path,
    command: list[str],
    source_run_id: str | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    summary: str | None = None,
    stream_output: bool = True,
    runner: str | None = None,
) -> dict:
    if source_run_id is None:
        if not repo or pr_number is None:
            raise ValueError("attempt_repair requires either source_run_id or repo/pr_number")
        validation = validate_github_pr(
            store,
            repo=repo,
            pr_number=pr_number,
            cwd=cwd,
            summary=f"attempt-repair validation for {repo}#{pr_number}",
        )
        source_run_id = str(validation["run_id"])

    adapter = select_runner_adapter(cwd, command=command, preferred=runner)
    applied_manifest = apply_interventions(store, source_run_id, cwd, adapter=adapter)
    replay_result = replay_run(
        store,
        source_run_id=source_run_id,
        cwd=cwd,
        command=command,
        summary=summary or f"Attempt repair for {source_run_id}",
        apply_interventions_first=False,
        stream_output=stream_output,
        runner=adapter.name,
    )
    comparison_row = store.get_replay_source_for_run(str(replay_result["run_id"]))
    comparison = json.loads(comparison_row["comparison_json"]) if comparison_row else None
    return {
        "source_run_id": source_run_id,
        "applied_manifest": applied_manifest,
        "replay_run_id": str(replay_result["run_id"]),
        "runner_adapter": adapter.name,
        "exit_code": replay_result["exit_code"],
        "comparison": comparison,
    }


def build_prompt_export(findings: list, interventions: list) -> str:
    lines = [
        "# AfterAction Prompt Export",
        "",
        "Use these constraints before making changes:",
        "",
    ]
    for finding in findings:
        lines.append(f"- {finding.title}: {finding.summary}")
    for intervention in interventions:
        if intervention.type == "prompt_patch":
            lines.extend(["", intervention.content])
    return "\n".join(lines).strip() + "\n"


def build_context_export(context: dict, related: list[dict]) -> str:
    gh_context = context["gh_context"]
    lines = [
        "# AfterAction Runner Context",
        "",
        f"Run: {context['run'].id}",
        f"Repo: {gh_context.get('repo', 'unknown')}",
        f"PR: {gh_context.get('pr_number', 'n/a')}",
        f"Failure files: {', '.join(sorted(context['failure_files'])) or 'none'}",
        f"Unresolved review files: {', '.join(sorted(context['unresolved_comment_paths'])) or 'none'}",
        "",
    ]
    if related:
        lines.append("Related runs:")
        for item in related[:5]:
            lines.append(
                f"- {item['run'].id} {item['run'].status} exit={item['run'].exit_code} summary={item['run'].summary or ''}"
            )
    return "\n".join(lines).strip() + "\n"


def build_runner_policy(interventions: list) -> dict:
    return {
        "generated_at": now_utc(),
        "rules": [
            {
                "type": intervention.type,
                "title": intervention.title,
                "target": intervention.target,
                "content": intervention.content,
                "scope": getattr(intervention, "scope", "pr"),
            }
            for intervention in interventions
            if intervention.type in {"runtime_guardrail", "tool_policy_rule", "context_injection_rule"}
        ],
    }


def build_replay_context(
    run_id: str,
    command: str,
    context: dict,
    related: list[dict],
    interventions: list,
) -> dict:
    gh_context = context["gh_context"]
    return {
        "source_run_id": run_id,
        "source_command": command,
        "repo": gh_context.get("repo"),
        "pr_number": gh_context.get("pr_number"),
        "failure_files": sorted(context["failure_files"]),
        "instruction_targets": resolve_instruction_target_names(interventions),
        "related_runs": [
            {
                "id": item["run"].id,
                "status": item["run"].status,
                "summary": item["run"].summary,
            }
            for item in related[:5]
        ],
        "intervention_titles": [intervention.title for intervention in interventions],
    }


def build_instruction_patches(
    base_dir: Path,
    run_id: str,
    interventions: list,
    target_paths: list[Path],
    context: dict,
) -> dict[str, str]:
    scoped_entries = [
        {
            "content": intervention.content,
            "scope": getattr(intervention, "scope", "pr"),
        }
        for intervention in interventions
        if intervention.type == "instruction_patch" and is_repo_instruction_target(intervention.target)
    ]
    if not scoped_entries:
        return {}
    preview_set = {
        "id": "preview",
        "source_run_id": run_id,
        "version": 0,
        "applied_at": now_utc(),
        "source_repo": context["gh_context"].get("repo"),
        "source_pr_number": context["gh_context"].get("pr_number"),
        "instructions": dedupe_instruction_entries(scoped_entries),
    }
    patches: dict[str, str] = {}
    for target_path in target_paths:
        existing = target_path.read_text() if target_path.exists() else ""
        updated = render_instruction_file(existing, [preview_set])
        diff = difflib.unified_diff(
            existing.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(target_path),
            tofile=str(target_path),
        )
        patch_text = "".join(diff)
        if patch_text:
            patches[target_path.name] = patch_text
    return patches


def render_instruction_file(existing: str, active_sets: list[dict]) -> str:
    block_lines = [AFTERACTION_START, "## AfterAction Interventions", ""]
    repo_sets = scoped_instruction_sets(active_sets, "repo")
    pr_sets = scoped_instruction_sets(active_sets, "pr")
    if repo_sets:
        block_lines.append("### Repo-wide Guidance")
        block_lines.append("")
        append_instruction_sets(block_lines, repo_sets, include_pr_context=False)
    if pr_sets:
        block_lines.append("### Active PR Guidance")
        block_lines.append("")
        append_instruction_sets(block_lines, pr_sets, include_pr_context=True)
    block_lines.append(AFTERACTION_END)
    block = "\n".join(block_lines)
    if AFTERACTION_START in existing and AFTERACTION_END in existing:
        prefix, remainder = existing.split(AFTERACTION_START, 1)
        _, suffix = remainder.split(AFTERACTION_END, 1)
        normalized = prefix.rstrip()
        if normalized:
            normalized += "\n\n"
        normalized += block + suffix
        return normalized if normalized.endswith("\n") else normalized + "\n"
    normalized_existing = existing.rstrip()
    if normalized_existing:
        normalized_existing += "\n\n"
    normalized_existing += block
    return normalized_existing if normalized_existing.endswith("\n") else normalized_existing + "\n"


def append_instruction_sets(block_lines: list[str], items: list[dict], include_pr_context: bool) -> None:
    for item in items:
        block_lines.append(
            f"#### Set v{item['version']} · run `{item['source_run_id']}` · set `{item['id']}`"
        )
        if include_pr_context and item.get("source_repo") and item.get("source_pr_number"):
            block_lines.append(f"Context: {item['source_repo']}#{item['source_pr_number']}")
        block_lines.append(f"Applied at: {item['applied_at']}")
        for entry in item["instructions"]:
            block_lines.append(f"- {entry['content']}")
        if include_pr_context:
            block_lines.append("  Expires when a newer PR-scoped set is applied for this repo.")
        block_lines.append("")


def scoped_instruction_sets(active_sets: list[dict], scope: str) -> list[dict]:
    scoped_sets = []
    for item in active_sets:
        entries = [entry for entry in item["instructions"] if entry["scope"] == scope]
        if not entries:
            continue
        scoped_sets.append({**item, "instructions": entries})
    return scoped_sets


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def dedupe_instruction_entries(entries: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for entry in entries:
        signature = (entry["scope"], entry["content"])
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(entry)
    return deduped


def intervention_output_dir(root: Path, run_id: str, version: int, set_id: str) -> Path:
    return root / run_id / f"v{version}-{set_id}"


def copy_manifest_outputs(source_dir: Path, destination_dir: Path) -> None:
    for path in source_dir.iterdir():
        if path.is_file():
            (destination_dir / path.name).write_text(path.read_text())


def rewrite_manifest_paths(exports: dict, destination_dir: Path) -> dict:
    rewritten = {}
    for key, value in exports.items():
        if isinstance(value, dict):
            rewritten[key] = {
                child_key: str(destination_dir / Path(child_value).name)
                for child_key, child_value in value.items()
            }
            continue
        rewritten[key] = str(destination_dir / Path(value).name)
    return rewritten


def active_instruction_sets(store: Store) -> list[dict]:
    sets = []
    for row in store.list_active_applied_intervention_sets():
        manifest = json.loads(row["manifest_json"])
        instructions = repo_instruction_entries(manifest.get("interventions", []))
        if not instructions:
            continue
        sets.append(
            {
                "id": row["id"],
                "source_run_id": row["source_run_id"],
                "version": row["version"],
                "applied_at": row["applied_at"],
                "source_repo": manifest.get("context", {}).get("repo"),
                "source_pr_number": manifest.get("context", {}).get("pr_number"),
                "instructions": dedupe_instruction_entries(instructions),
            }
        )
    return sets


def latest_export_manifest(store: Store, run_id: str) -> dict | None:
    for row in store.list_intervention_sets_for_run(run_id):
        if row["kind"] != "export" or row["applied_at"] is not None:
            continue
        return json.loads(row["manifest_json"])
    return None


def stale_set_ids(active_sets: list[dict], current_set_id: str) -> list[str]:
    current = next((item for item in active_sets if item["id"] == current_set_id), None)
    seen_signatures: dict[tuple[tuple[str, str], ...], str] = {}
    stale: list[str] = []
    for item in reversed(active_sets):
        signature = tuple((entry["scope"], entry["content"]) for entry in item["instructions"])
        if signature in seen_signatures:
            stale.append(item["id"])
        else:
            seen_signatures[signature] = item["id"]
    if current and current.get("source_repo") and current.get("source_pr_number"):
        for item in active_sets:
            if item["id"] == current_set_id:
                continue
            if not any(entry["scope"] == "pr" for entry in item["instructions"]):
                continue
            if item.get("source_repo") != current["source_repo"]:
                continue
            if item.get("source_pr_number") == current["source_pr_number"]:
                continue
            stale.append(item["id"])
    return stale


def build_instruction_patch_from_sets(existing: str, target_path: Path, active_sets: list[dict]) -> str:
    updated = render_instruction_file(existing, active_sets)
    diff = difflib.unified_diff(
        existing.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=str(target_path),
        tofile=str(target_path),
    )
    return "".join(diff)


def compare_runs(store: Store, source_run_id: str, replay_run_id: str) -> dict:
    source_run = store.get_run(source_run_id)
    replay_run = store.get_run(replay_run_id)
    if not source_run or not replay_run:
        raise ValueError("Cannot compare missing runs")
    source_context = load_run_context(store, source_run_id)
    replay_context = load_run_context(store, replay_run_id)
    source_findings, _ = analyze_run(store, source_run_id)
    replay_findings, _ = analyze_run(store, replay_run_id)
    source_codes = {finding.code for finding in source_findings}
    replay_codes = {finding.code for finding in replay_findings}
    resolved_codes = sorted(source_codes - replay_codes)
    persisted_codes = sorted(source_codes & replay_codes)
    new_codes = sorted(replay_codes - source_codes)
    source_checks = failing_check_count(source_context["gh_context"])
    replay_checks = failing_check_count(replay_context["gh_context"])
    source_failure_files = sorted(source_context["failure_files"])
    replay_failure_files = sorted(replay_context["failure_files"])
    source_unresolved_review_files = sorted(source_context["unresolved_comment_paths"])
    replay_unresolved_review_files = sorted(replay_context["unresolved_comment_paths"])
    source_surface = source_context["failure_files"] | source_context["unresolved_comment_paths"]
    replay_surface = replay_context["failure_files"] | replay_context["unresolved_comment_paths"]
    source_overlap_count = len(source_context["analysis_files"] & source_surface)
    replay_overlap_count = len(replay_context["analysis_files"] & replay_surface)
    status_gain = 40 if source_run.status != "passed" and replay_run.status == "passed" else 0
    status_loss = -40 if source_run.status == "passed" and replay_run.status != "passed" else 0
    finding_score = (len(resolved_codes) * 12) - (len(new_codes) * 12)
    failure_file_score = (len(source_failure_files) - len(replay_failure_files)) * 5
    ci_score = (source_checks - replay_checks) * 6
    review_resolution_score = (len(source_unresolved_review_files) - len(replay_unresolved_review_files)) * 4
    overlap_improvement_score = (replay_overlap_count - source_overlap_count) * 7
    score = (
        status_gain
        + status_loss
        + finding_score
        + failure_file_score
        + ci_score
        + review_resolution_score
        + overlap_improvement_score
    )
    improved = score > 0 or (source_run.status != "passed" and replay_run.status == "passed")
    regressed = score < 0
    verdict = "improved" if improved else "regressed" if regressed else "unchanged"
    return {
        "source_status": source_run.status,
        "replay_status": replay_run.status,
        "source_exit_code": source_run.exit_code,
        "replay_exit_code": replay_run.exit_code,
        "source_findings": len(source_findings),
        "replay_findings": len(replay_findings),
        "resolved_findings": resolved_codes,
        "persisted_findings": persisted_codes,
        "new_findings": new_codes,
        "source_failure_files": source_failure_files,
        "replay_failure_files": replay_failure_files,
        "source_failing_checks": source_checks,
        "replay_failing_checks": replay_checks,
        "source_unresolved_review_files": source_unresolved_review_files,
        "replay_unresolved_review_files": replay_unresolved_review_files,
        "source_overlap_count": source_overlap_count,
        "replay_overlap_count": replay_overlap_count,
        "review_resolution_score": review_resolution_score,
        "overlap_improvement_score": overlap_improvement_score,
        "score": score,
        "verdict": verdict,
        "improved": improved,
    }


def failing_check_count(gh_context: dict) -> int:
    return sum(1 for check in gh_context.get("checks", []) if (check.get("bucket") or "").lower() == "fail")


def repo_instruction_entries(interventions: list[dict]) -> list[dict]:
    entries = []
    for intervention in interventions:
        if intervention["type"] != "instruction_patch":
            continue
        if not is_repo_instruction_target(intervention["target"]):
            continue
        entries.append(
            {
                "content": intervention["content"],
                "scope": intervention.get("scope", "pr"),
            }
        )
    return entries


def is_repo_instruction_target(target: str) -> bool:
    return target == REPO_INSTRUCTION_TARGET or target in KNOWN_INSTRUCTION_FILES


def resolve_instruction_targets(
    base_dir: Path,
    interventions: list,
    adapter: RunnerAdapter | None = None,
) -> list[Path]:
    explicit_targets = []
    repo_target_requested = False
    for intervention in interventions:
        target = intervention["target"] if isinstance(intervention, dict) else intervention.target
        if target in KNOWN_INSTRUCTION_FILES:
            explicit_targets.append(base_dir / target)
        elif target == REPO_INSTRUCTION_TARGET:
            repo_target_requested = True
    if explicit_targets:
        return sort_instruction_paths(explicit_targets)
    if not repo_target_requested:
        return []
    active_adapter = adapter or ShellAdapter()
    return active_adapter.instruction_targets(base_dir)


def resolve_instruction_target_names(interventions: list) -> list[str]:
    names = []
    for intervention in interventions:
        target = intervention.target
        if target == REPO_INSTRUCTION_TARGET:
            if DEFAULT_INSTRUCTION_FILE not in names:
                names.append(DEFAULT_INSTRUCTION_FILE)
            continue
        if target in KNOWN_INSTRUCTION_FILES and target not in names:
            names.append(target)
    return names


def sort_instruction_paths(paths: list[Path]) -> list[Path]:
    order = {name: index for index, name in enumerate(KNOWN_INSTRUCTION_FILES)}
    return sorted(paths, key=lambda path: (order.get(path.name, len(order)), path.name))


def adapter_for_manifest(base_dir: Path, manifest: dict) -> RunnerAdapter:
    adapter_name = manifest.get("runner_adapter")
    if adapter_name:
        for adapter in ADAPTERS:
            if adapter.name == adapter_name:
                return adapter
    for adapter in ADAPTERS:
        if adapter.detect(base_dir):
            return adapter
    return ShellAdapter()
