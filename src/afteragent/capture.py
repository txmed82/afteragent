from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .adapters import RunnerAdapter, select_runner_adapter
from .github import capture_github_context
from .models import now_utc
from .store import Store
from .transcripts import SOURCE_STDOUT_HEURISTIC, make_parse_error


def run_command(
    store: Store,
    command: list[str],
    cwd: Path,
    summary: str | None = None,
    github_repo: str | None = None,
    github_pr: int | None = None,
    stream_output: bool = True,
    extra_env: dict[str, str] | None = None,
    adapter: RunnerAdapter | None = None,
    task_prompt: str | None = None,  # NEW
) -> dict[str, str | int]:
    active_adapter = adapter or select_runner_adapter(cwd, command=command)

    # Three-tier task prompt resolution: explicit kwarg > adapter parse > full command.
    if task_prompt is not None:
        resolved_task_prompt = task_prompt
    else:
        parsed = active_adapter.parse_task_prompt(command)
        resolved_task_prompt = parsed if parsed is not None else shlex.join(command)
    run_id = uuid.uuid4().hex[:12]
    command_text = shlex.join(command)
    created_at = now_utc()
    store.create_run(
        run_id,
        command_text,
        str(cwd),
        created_at,
        summary=summary or "Captured by afteragent exec",
    )
    store.set_run_task_prompt(run_id, resolved_task_prompt)
    store.add_event(
        run_id,
        "run.started",
        created_at,
        {
            "command": command,
            "cwd": str(cwd),
            "runner_adapter": active_adapter.name,
            "extra_env_keys": sorted(extra_env.keys()) if extra_env else [],
        },
    )
    # Snapshot runner-specific pre-launch state. Defensive guard matches the
    # spec's error-handling contract: if the adapter raises (permission on
    # ~/.claude/projects/, RuntimeError from Path.home() in a stripped env,
    # or a buggy subclass), fall through with an empty state so post-exit
    # resolution sees zero candidates and the generic fallback parser runs.
    try:
        pre_launch_state = active_adapter.pre_launch_snapshot(cwd)
    except Exception:
        pre_launch_state = {}

    artifact_dir = store.run_artifact_dir(run_id)
    before_diff = capture_git_diff(cwd)
    (artifact_dir / "git_diff_before.patch").write_text(before_diff)
    store.add_event(
        run_id,
        "artifact.captured",
        now_utc(),
        {"name": "git_diff_before.patch", "bytes": len(before_diff.encode())},
    )

    start = time.time()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, **(extra_env or {})},
        )
    except OSError as exc:
        return handle_spawn_failure(
            store=store,
            run_id=run_id,
            cwd=cwd,
            artifact_dir=artifact_dir,
            before_diff=before_diff,
            started_at=start,
            error=exc,
        )
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    reader_threads = [
        threading.Thread(
            target=_stream_pipe,
            args=(process.stdout, stdout_buffer, sys.stdout, stream_output),
            daemon=True,
        ),
        threading.Thread(
            target=_stream_pipe,
            args=(process.stderr, stderr_buffer, sys.stderr, stream_output),
            daemon=True,
        ),
    ]
    for thread in reader_threads:
        thread.start()
    return_code = process.wait()
    for thread in reader_threads:
        thread.join()
    duration_ms = int((time.time() - start) * 1000)
    stdout_text = stdout_buffer.getvalue()
    stderr_text = stderr_buffer.getvalue()

    stdout_path = artifact_dir / "stdout.log"
    stderr_path = artifact_dir / "stderr.log"
    stdout_path.write_text(stdout_text)
    stderr_path.write_text(stderr_text)
    parsed_events = active_adapter.parse_transcript_events(stdout_text, stderr_text, artifact_dir)
    for parsed in parsed_events:
        store.add_event(run_id, parsed["event_type"], now_utc(), parsed["payload"])

    # New transcript ingestion layer (sub-project 1). Additive — does not
    # replace the legacy parse_transcript_events path above.
    transcripts_dir = artifact_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    try:
        transcript_events = active_adapter.parse_transcript(
            run_id=run_id,
            artifact_dir=artifact_dir,
            stdout=stdout_text,
            stderr=stderr_text,
            pre_launch_state=pre_launch_state,
        )
    except Exception as exc:
        # Contract says parsers never raise; defend against a buggy adapter.
        transcript_events = [
            make_parse_error(
                run_id=run_id,
                sequence=0,
                source=SOURCE_STDOUT_HEURISTIC,
                message=f"adapter parse_transcript raised: {exc}",
                raw_ref=None,
            )
        ]
    store.add_transcript_events(run_id, transcript_events)

    store.add_event(
        run_id,
        "process.completed",
        now_utc(),
        {
            "exit_code": return_code,
            "stdout_artifact": str(stdout_path.relative_to(store.paths.root)),
            "stderr_artifact": str(stderr_path.relative_to(store.paths.root)),
        },
    )

    after_diff = capture_git_diff(cwd)
    (artifact_dir / "git_diff_after.patch").write_text(after_diff)
    diff_stats = diff_summary(before_diff, after_diff)
    store.add_event(
        run_id,
        "artifact.captured",
        now_utc(),
        {
            "name": "git_diff_after.patch",
            "bytes": len(after_diff.encode()),
            "changed_files": diff_stats["changed_files"],
        },
    )

    gh_context = capture_github_context(cwd, artifact_dir, repo=github_repo, pr_number=github_pr)
    if gh_context:
        store.add_event(run_id, "github.context", now_utc(), gh_context)

    status = "failed" if return_code else "passed"
    summary = (
        f"Exit {return_code}; changed_files={diff_stats['changed_files']}; "
        f"stdout_lines={line_count(stdout_text)}; stderr_lines={line_count(stderr_text)}"
    )
    store.finish_run(run_id, status, return_code, now_utc(), duration_ms, summary=summary)
    store.add_event(
        run_id,
        "run.finished",
        now_utc(),
        {"status": status, "duration_ms": duration_ms},
    )
    return {
        "run_id": run_id,
        "exit_code": return_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def validate_github_pr(
    store: Store,
    repo: str,
    pr_number: int,
    cwd: Path,
    summary: str | None = None,
) -> dict[str, str | int]:
    run_id = uuid.uuid4().hex[:12]
    created_at = now_utc()
    command_text = f"github-pr-validation {repo}#{pr_number}"
    store.create_run(
        run_id,
        command_text,
        str(cwd),
        created_at,
        summary=summary or "GitHub PR validation run",
    )
    store.add_event(
        run_id,
        "run.started",
        created_at,
        {"mode": "github_validation", "repo": repo, "pr_number": pr_number},
    )

    artifact_dir = store.run_artifact_dir(run_id)
    for name in ("stdout.log", "stderr.log", "git_diff_before.patch", "git_diff_after.patch"):
        (artifact_dir / name).write_text("")
    gh_context = capture_github_context(cwd, artifact_dir, repo=repo, pr_number=pr_number)
    if not gh_context:
        store.finish_run(
            run_id,
            "failed",
            1,
            now_utc(),
            0,
            summary="Unable to capture GitHub PR context",
        )
        store.add_event(
            run_id,
            "run.finished",
            now_utc(),
            {"status": "failed", "duration_ms": 0, "reason": "missing_github_context"},
        )
        return {"run_id": run_id, "exit_code": 1}

    store.add_event(run_id, "github.context", now_utc(), gh_context)
    status, exit_code = github_validation_status(gh_context)
    store.finish_run(
        run_id,
        status,
        exit_code,
        now_utc(),
        0,
        summary=github_validation_summary(gh_context),
    )
    store.add_event(
        run_id,
        "run.finished",
        now_utc(),
        {"status": status, "duration_ms": 0, "mode": "github_validation"},
    )
    return {"run_id": run_id, "exit_code": exit_code}


def capture_git_diff(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout

def diff_summary(before_diff: str, after_diff: str) -> dict[str, int]:
    before_files = changed_files(before_diff)
    after_files = changed_files(after_diff)
    net_new = after_files - before_files
    return {
        "changed_files": len(after_files),
        "new_files_touched": len(net_new),
    }


def changed_files(diff_text: str) -> set[str]:
    files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.add(parts[2].removeprefix("a/"))
    return files


def line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _stream_pipe(
    pipe: io.TextIOBase | None,
    buffer: io.StringIO,
    output_stream: io.TextIOBase,
    stream_output: bool,
) -> None:
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            buffer.write(line)
            if stream_output:
                output_stream.write(line)
                output_stream.flush()
    finally:
        pipe.close()


def handle_spawn_failure(
    store: Store,
    run_id: str,
    cwd: Path,
    artifact_dir: Path,
    before_diff: str,
    started_at: float,
    error: OSError,
) -> dict[str, str | int]:
    duration_ms = int((time.time() - started_at) * 1000)
    stdout_path = artifact_dir / "stdout.log"
    stderr_path = artifact_dir / "stderr.log"
    stdout_path.write_text("")
    stderr_path.write_text(f"{type(error).__name__}: {error}\n")
    store.add_event(
        run_id,
        "process.spawn_failed",
        now_utc(),
        {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "stdout_artifact": str(stdout_path.relative_to(store.paths.root)),
            "stderr_artifact": str(stderr_path.relative_to(store.paths.root)),
        },
    )
    after_diff = capture_git_diff(cwd)
    (artifact_dir / "git_diff_after.patch").write_text(after_diff)
    diff_stats = diff_summary(before_diff, after_diff)
    store.add_event(
        run_id,
        "artifact.captured",
        now_utc(),
        {
            "name": "git_diff_after.patch",
            "bytes": len(after_diff.encode()),
            "changed_files": diff_stats["changed_files"],
        },
    )
    exit_code = spawn_error_exit_code(error)
    summary = f"Spawn failed: {type(error).__name__}: {error}"
    store.finish_run(run_id, "failed", exit_code, now_utc(), duration_ms, summary=summary)
    store.add_event(
        run_id,
        "run.finished",
        now_utc(),
        {
            "status": "failed",
            "duration_ms": duration_ms,
            "reason": "spawn_failed",
            "exit_code": exit_code,
        },
    )
    return {
        "run_id": run_id,
        "exit_code": exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def spawn_error_exit_code(error: OSError) -> int:
    if isinstance(error, FileNotFoundError):
        return 127
    if isinstance(error, PermissionError):
        return 126
    return 1


def github_validation_status(gh_context: dict) -> tuple[str, int]:
    checks = gh_context.get("checks", [])
    buckets = {(check.get("bucket") or "").lower() for check in checks}
    if "fail" in buckets:
        return "failed", 1
    if "pending" in buckets:
        return "running", 0
    ci_runs = gh_context.get("ci_runs", [])
    conclusions = {(run.get("conclusion") or "").lower() for run in ci_runs}
    if "failure" in conclusions:
        return "failed", 1
    if any((run.get("status") or "").lower() not in {"completed", ""} for run in ci_runs):
        return "running", 0
    return "passed", 0


def github_validation_summary(gh_context: dict) -> str:
    review_summary = gh_context.get("review_summary", {})
    checks = gh_context.get("checks", [])
    failing_checks = [check.get("name") for check in checks if (check.get("bucket") or "").lower() == "fail"]
    pending_checks = [check.get("name") for check in checks if (check.get("bucket") or "").lower() == "pending"]
    return (
        f"repo={gh_context.get('repo')} pr={gh_context.get('pr_number')}; "
        f"unresolved_threads={review_summary.get('unresolved_thread_count', 0)}; "
        f"failing_checks={len(failing_checks)}; pending_checks={len(pending_checks)}"
    )
