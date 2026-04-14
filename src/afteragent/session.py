from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path

from .compression import build_context_bundle
from .diagnostics import analyze_run
from .memory import create_memories_for_run, retrieve_memories
from .models import Intervention, PatternFinding, now_utc
from .recommendations import Recommendation, recommend_tools
from .store import Store
from .transcripts import (
    KIND_ASSISTANT_MESSAGE,
    KIND_BASH_COMMAND,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PARSE_ERROR,
    KIND_SEARCH,
    KIND_TEST_RUN,
    KIND_TODO_UPDATE,
    KIND_UNKNOWN,
    KIND_USER_MESSAGE,
    KIND_WEB_FETCH,
    SOURCE_STDOUT_HEURISTIC,
    TranscriptEvent,
)
from .workflow import apply_interventions


_EVENT_KIND_MAP = {
    "message": KIND_ASSISTANT_MESSAGE,
    "tool.called": KIND_UNKNOWN,
    "tool.result": KIND_UNKNOWN,
    "file.read": KIND_FILE_READ,
    "file.edited": KIND_FILE_EDIT,
    "command.started": KIND_BASH_COMMAND,
    "command.finished": KIND_BASH_COMMAND,
    "plan.updated": KIND_TODO_UPDATE,
    "error": KIND_PARSE_ERROR,
    "search": KIND_SEARCH,
    "web.fetch": KIND_WEB_FETCH,
    "user.message": KIND_USER_MESSAGE,
}

_TEST_COMMAND_TOKENS = ("pytest", "test", "jest", "vitest", "cargo test", "go test", "rspec")


def _event_to_transcript(run_id: str, sequence: int, payload: dict) -> TranscriptEvent:
    event_type = payload.get("event_type", "message")
    status = payload.get("status", "unknown")
    tool_name = payload.get("tool_name")
    target = payload.get("target")
    inputs_summary = payload.get("inputs_summary", payload.get("input", ""))
    output_excerpt = payload.get("output_excerpt", payload.get("output", payload.get("message", "")))
    if event_type == "command.finished" and payload.get("exit_code") not in (None, 0):
        status = "error"
    elif event_type == "command.finished" and payload.get("exit_code") == 0:
        status = "success"
    kind = _EVENT_KIND_MAP.get(event_type, KIND_UNKNOWN)
    if kind == KIND_BASH_COMMAND and target:
        lowered = str(target).lower()
        if any(token in lowered for token in _TEST_COMMAND_TOKENS):
            kind = KIND_TEST_RUN
    if event_type == "message" and payload.get("role") == "user":
        kind = KIND_USER_MESSAGE
    return TranscriptEvent(
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        tool_name=tool_name,
        target=target,
        inputs_summary=str(inputs_summary or ""),
        output_excerpt=str(output_excerpt or ""),
        status=status,
        source=payload.get("source", "mcp"),
        timestamp=payload.get("timestamp", now_utc()),
        raw_ref=payload.get("raw_ref"),
    )


def _append_text(path: Path, text: str) -> None:
    if not text:
        return
    # Use append mode to avoid O(n²) behavior
    needs_newline = False
    if path.exists():
        # Check if we need a separator newline
        with path.open("r", encoding="utf-8") as f:
            try:
                # Seek to end and read last character
                f.seek(max(0, path.stat().st_size - 1))
                last_char = f.read()
                needs_newline = last_char and not last_char.endswith("\n")
            except (OSError, UnicodeDecodeError):
                needs_newline = True
    with path.open("a", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        f.write(text)


def start_run(
    store: Store,
    *,
    cwd: Path,
    task_prompt: str,
    client_name: str | None = None,
    repo_context: dict | None = None,
) -> dict:
    run_id = uuid.uuid4().hex[:12]
    created_at = now_utc()
    store.create_run(
        run_id,
        command=f"mcp-session {client_name or 'client'}",
        cwd=str(cwd),
        created_at=created_at,
        summary="MCP session started",
        client_name=client_name,
        lifecycle_status="active",
    )
    store.set_run_task_prompt(run_id, task_prompt)
    store.add_event(
        run_id,
        "run.started",
        created_at,
        {"mode": "mcp", "client_name": client_name, "task_prompt": task_prompt},
    )
    artifact_dir = store.run_artifact_dir(run_id)
    for name in ("stdout.log", "stderr.log", "git_diff_before.patch", "git_diff_after.patch"):
        target = artifact_dir / name
        if not target.exists():
            target.write_text("")
    if repo_context:
        (artifact_dir / "github_context.json").write_text(json.dumps(repo_context, indent=2))
    memories = retrieve_memories(store, run_id, task_prompt)
    return {"run_id": run_id, "memories": memories}


def append_events(store: Store, run_id: str, events: list[dict]) -> dict:
    # Compute sequences and insert within a single transaction to avoid races
    with store.connection() as conn:
        # Get max sequence atomically within the transaction
        cursor = conn.execute(
            "SELECT MAX(sequence) as max_seq FROM transcript_events WHERE run_id = ?",
            (run_id,)
        )
        row = cursor.fetchone()
        base_sequence = (row["max_seq"] + 1) if (row and row["max_seq"] is not None) else 0

        transcript_events: list[TranscriptEvent] = []
        artifact_dir = store.run_artifact_dir(run_id)
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        for offset, payload in enumerate(events):
            sequence = base_sequence + offset
            transcript = _event_to_transcript(run_id, sequence, payload)
            transcript_events.append(transcript)
            # Insert event within same transaction
            payload_json = json.dumps(payload)
            conn.execute(
                "INSERT INTO events (run_id, event_type, timestamp, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, payload.get("event_type", "mcp.event"), transcript.timestamp or now_utc(), payload_json)
            )
            if transcript.status == "error" or payload.get("stream") == "stderr":
                stderr_lines.append(transcript.output_excerpt or transcript.inputs_summary)
            else:
                stdout_lines.append(transcript.output_excerpt or transcript.inputs_summary)

        # Insert transcript events within same transaction
        if transcript_events:
            conn.executemany(
                """
                INSERT INTO transcript_events (
                    run_id, sequence, kind, tool_name, target,
                    inputs_summary, output_excerpt, status, source, timestamp, raw_ref
                )
                VALUES (
                    :run_id, :sequence, :kind, :tool_name, :target,
                    :inputs_summary, :output_excerpt, :status, :source, :timestamp, :raw_ref
                )
                """,
                [
                    {
                        "run_id": ev.run_id,
                        "sequence": ev.sequence,
                        "kind": ev.kind,
                        "tool_name": ev.tool_name,
                        "target": ev.target,
                        "inputs_summary": ev.inputs_summary,
                        "output_excerpt": ev.output_excerpt,
                        "status": ev.status,
                        "source": ev.source,
                        "timestamp": ev.timestamp,
                        "raw_ref": ev.raw_ref,
                    }
                    for ev in transcript_events
                ]
            )

    _append_text(artifact_dir / "stdout.log", "\n".join(line for line in stdout_lines if line))
    _append_text(artifact_dir / "stderr.log", "\n".join(line for line in stderr_lines if line))
    return {"appended": len(transcript_events)}


def attach_context(store: Store, run_id: str, context: dict) -> None:
    artifact_dir = store.run_artifact_dir(run_id)
    if "github_context" in context:
        (artifact_dir / "github_context.json").write_text(json.dumps(context["github_context"], indent=2))
    if "stdout" in context:
        _append_text(artifact_dir / "stdout.log", str(context["stdout"]))
    if "stderr" in context:
        _append_text(artifact_dir / "stderr.log", str(context["stderr"]))
    if "git_diff" in context:
        (artifact_dir / "git_diff_after.patch").write_text(str(context["git_diff"]))
    store.add_event(run_id, "context.attached", now_utc(), {"keys": sorted(context.keys())})


def _compute_run_exit(store: Store, run_id: str) -> int:
    transcript_events = store.get_transcript_events(run_id)
    if any(event.status == "error" for event in transcript_events):
        return 1
    return 0


def _build_summary(store: Store, run_id: str) -> str:
    transcript_events = store.get_transcript_events(run_id)
    edits = len([event for event in transcript_events if event.kind == KIND_FILE_EDIT])
    commands = len([event for event in transcript_events if event.kind in {KIND_BASH_COMMAND, KIND_TEST_RUN}])
    errors = len([event for event in transcript_events if event.status == "error"])
    return f"MCP session finalized; edits={edits}; commands={commands}; errors={errors}"


def _compression_blocks(store: Store, run_id: str) -> list[tuple[str, str]]:
    artifact_dir = store.run_artifact_dir(run_id)
    transcript = store.get_transcript_events(run_id)
    transcript_text = "\n".join(
        f"{event.sequence}: {event.kind} {event.target or ''} {event.output_excerpt or event.inputs_summary}".strip()
        for event in transcript
    )
    return [
        ("stdout", (artifact_dir / "stdout.log").read_text() if (artifact_dir / "stdout.log").exists() else ""),
        ("stderr", (artifact_dir / "stderr.log").read_text() if (artifact_dir / "stderr.log").exists() else ""),
        ("transcript", transcript_text),
        ("git_diff", (artifact_dir / "git_diff_after.patch").read_text() if (artifact_dir / "git_diff_after.patch").exists() else ""),
    ]


def _store_compression_results(store: Store, run_id: str, results) -> list[dict]:
    rows: list[dict] = []
    for result in results:
        store.save_compressed_artifact(
            run_id=run_id,
            artifact_kind=result.artifact_kind,
            artifact_name=result.artifact_kind,
            original_text=result.original_text,
            compressed_text=result.compressed_text,
            strategy=result.strategy,
            preserved_count=result.preserved_count,
            created_at=now_utc(),
            fallback_reason=result.fallback_reason,
        )
        rows.append(
            {
                "artifact_kind": result.artifact_kind,
                "strategy": result.strategy,
                "original_size": result.original_size,
                "compressed_size": result.compressed_size,
                "estimated_original_tokens": result.estimated_original_tokens,
                "estimated_compressed_tokens": result.estimated_compressed_tokens,
                "preserved_count": result.preserved_count,
                "fallback_reason": result.fallback_reason,
            }
        )
    return rows


def _ensure_pending_actions(
    store: Store,
    run_id: str,
    findings: list[PatternFinding],
    interventions: list[Intervention],
    recommendations: list[Recommendation],
) -> list[dict]:
    existing = store.list_pending_actions(run_id)
    if existing:
        return [
            {
                "id": action.id,
                "type": action.action_type,
                "title": action.title,
                "status": action.status,
                "payload": json.loads(action.payload_json),
            }
            for action in existing
        ]

    created: list[dict] = []
    if any(item.target == "repo_instructions" for item in interventions):
        action_id = store.create_pending_action(
            run_id,
            action_type="apply_repo_instruction_patch",
            title="Apply repo instruction updates",
            payload={"run_id": run_id},
            created_at=now_utc(),
        )
        created.append(
            {"id": action_id, "type": "apply_repo_instruction_patch", "title": "Apply repo instruction updates", "status": "pending", "payload": {"run_id": run_id}}
        )

    for item in recommendations:
        if item.install_command:
            action_type = "install_skill" if item.kind == "skill" else "install_mcp"
            payload = {"command": item.install_command, "recommendation": item.title}
            action_id = store.create_pending_action(
                run_id,
                action_type=action_type,
                title=f"Install {item.title}",
                payload=payload,
                created_at=now_utc(),
            )
            created.append(
                {"id": action_id, "type": action_type, "title": f"Install {item.title}", "status": "pending", "payload": payload}
            )
    return created


def finalize_run(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    exit_code = _compute_run_exit(store, run_id)
    status = "failed" if exit_code else "passed"
    finished_at = now_utc()
    if run.status == "running":
        store.finish_run(run_id, status, exit_code, finished_at, 0, summary=_build_summary(store, run_id))
    store.update_run_lifecycle(run_id, "finalized", finalized_at=finished_at)

    bundle_text, compression_results = build_context_bundle(_compression_blocks(store, run_id))
    compression_report = _store_compression_results(store, run_id, compression_results)

    findings, interventions = analyze_run(store, run_id)
    recommendations = recommend_tools(findings, run.task_prompt or run.command)
    memories_created = create_memories_for_run(store, run_id, findings, interventions, bundle_text)
    pending_actions = _ensure_pending_actions(store, run_id, findings, interventions, recommendations)
    store.add_event(
        run_id,
        "run.finalized",
        finished_at,
        {
            "findings": len(findings),
            "interventions": len(interventions),
            "recommendations": len(recommendations),
            "memories_created": len(memories_created),
        },
    )
    return {
        "run_id": run_id,
        "status": status,
        "findings": [asdict(finding) for finding in findings],
        "interventions": [asdict(intervention) for intervention in interventions],
        "recommendations": [
            {
                "key": item.key,
                "kind": item.kind,
                "title": item.title,
                "rationale": item.rationale,
                "install_command": item.install_command,
                "setup_command": item.setup_command,
            }
            for item in recommendations
        ],
        "pending_actions": pending_actions,
        "memory_hits": [dict(row) for row in store.list_memory_hits(run_id)],
        "memories_created": memories_created,
        "compression_report": compression_report,
        "compressed_context": bundle_text,
    }


def approve_actions(
    store: Store,
    run_id: str,
    cwd: Path,
    action_ids: list[int] | None = None,
) -> list[dict]:
    # Get the run's recorded cwd instead of using the caller's
    run = store.get_run(run_id)
    if run:
        run_cwd = Path(run.cwd)
    else:
        # Fallback to provided cwd if run not found
        run_cwd = cwd

    actions = [action for action in store.list_pending_actions(run_id) if action.status == "pending"]
    if action_ids is not None:
        action_id_set = set(action_ids)
        actions = [action for action in actions if action.id in action_id_set]
    results: list[dict] = []
    for action in actions:
        payload = json.loads(action.payload_json)
        approved_at = now_utc()
        store.approve_pending_action(action.id, approved_at)
        if action.action_type == "apply_repo_instruction_patch":
            try:
                manifest = apply_interventions(store, run_id, run_cwd)
                result = {"applied_paths": manifest.get("applied_paths", [])}
                store.complete_pending_action(action.id, "completed", now_utc(), result)
            except Exception as e:
                result = {"ok": False, "reason": "exception", "error": str(e)}
                store.complete_pending_action(action.id, "failed", now_utc(), result)
        else:
            command = payload.get("command")
            if not command:
                result = {"ok": False, "reason": "no_command"}
                store.complete_pending_action(action.id, "skipped", now_utc(), result)
            else:
                try:
                    # Use shlex.split if command is a string
                    if isinstance(command, str):
                        import shlex
                        command_list = shlex.split(command)
                    else:
                        command_list = command
                    proc = subprocess.run(command_list, cwd=str(run_cwd), capture_output=True, text=True)
                    result = {
                        "ok": proc.returncode == 0,
                        "exit_code": proc.returncode,
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "command": command,
                    }
                    store.complete_pending_action(
                        action.id,
                        "completed" if proc.returncode == 0 else "failed",
                        now_utc(),
                        result,
                    )
                except Exception as e:
                    result = {"ok": False, "reason": "exception", "error": str(e)}
                    store.complete_pending_action(action.id, "failed", now_utc(), result)
        results.append({"id": action.id, "type": action.action_type, "title": action.title, "result": result})
    return results