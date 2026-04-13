from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import AppPaths
from .models import (
    CompressionArtifactRecord,
    EventRecord,
    MemoryRecord,
    PendingActionRecord,
    RunRecord,
    TranscriptEventRow,
)
from .transcripts import TranscriptEvent


class Store:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.paths.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_ms INTEGER,
                    summary TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS diagnoses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interventions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    target TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'pr'
                );

                CREATE TABLE IF NOT EXISTS intervention_sets (
                    id TEXT PRIMARY KEY,
                    source_run_id TEXT NOT NULL REFERENCES runs(id),
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    applied_at TEXT,
                    superseded_at TEXT
                );

                CREATE TABLE IF NOT EXISTS replay_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_run_id TEXT NOT NULL REFERENCES runs(id),
                    replay_run_id TEXT NOT NULL REFERENCES runs(id),
                    intervention_set_id TEXT NOT NULL REFERENCES intervention_sets(id),
                    created_at TEXT NOT NULL,
                    applied_before_replay INTEGER NOT NULL DEFAULT 0,
                    comparison_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transcript_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    tool_name TEXT,
                    target TEXT,
                    inputs_summary TEXT NOT NULL DEFAULT '',
                    output_excerpt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    source TEXT NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT '',
                    raw_ref TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_transcript_events_run_seq  ON transcript_events (run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_transcript_events_run_kind ON transcript_events (run_id, kind);

                CREATE TABLE IF NOT EXISTS llm_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    raw_response_excerpt TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_llm_generations_run ON llm_generations (run_id);

                CREATE TABLE IF NOT EXISTS pending_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    action_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    executed_at TEXT,
                    result_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_pending_actions_run ON pending_actions (run_id, status);

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    scope TEXT NOT NULL DEFAULT 'repo',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    link_type TEXT NOT NULL,
                    link_value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_links_memory ON memory_links (memory_id);
                CREATE INDEX IF NOT EXISTS idx_memory_links_lookup ON memory_links (link_type, link_value);

                CREATE TABLE IF NOT EXISTS memory_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_memory_hits_run ON memory_hits (run_id, score DESC);

                CREATE TABLE IF NOT EXISTS compressed_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    artifact_kind TEXT NOT NULL,
                    artifact_name TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    compressed_text TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    original_size INTEGER NOT NULL,
                    compressed_size INTEGER NOT NULL,
                    preserved_count INTEGER NOT NULL DEFAULT 0,
                    fallback_reason TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_compressed_artifacts_run ON compressed_artifacts (run_id, artifact_kind);
                """
            )
            self._ensure_column(conn, "interventions", "scope", "TEXT NOT NULL DEFAULT 'pr'")
            self._ensure_column(conn, "diagnoses", "source", "TEXT NOT NULL DEFAULT 'rule'")
            self._ensure_column(conn, "interventions", "source", "TEXT NOT NULL DEFAULT 'rule'")
            self._ensure_column(conn, "runs", "task_prompt", "TEXT")
            self._ensure_column(conn, "runs", "client_name", "TEXT")
            self._ensure_column(conn, "runs", "lifecycle_status", "TEXT NOT NULL DEFAULT 'finished'")
            self._ensure_column(conn, "runs", "finalized_at", "TEXT")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in columns:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(
        self,
        run_id: str,
        command: str,
        cwd: str,
        created_at: str,
        summary: str | None = None,
        client_name: str | None = None,
        lifecycle_status: str = "finished",
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, command, cwd, status, exit_code, created_at, summary, client_name, lifecycle_status
                )
                VALUES (?, ?, ?, 'running', NULL, ?, ?, ?, ?)
                """,
                (run_id, command, cwd, created_at, summary, client_name, lifecycle_status),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        exit_code: int,
        finished_at: str,
        duration_ms: int,
        summary: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, exit_code = ?, finished_at = ?, duration_ms = ?, summary = COALESCE(?, summary)
                WHERE id = ?
                """,
                (status, exit_code, finished_at, duration_ms, summary, run_id),
            )

    def set_run_task_prompt(self, run_id: str, task_prompt: str) -> None:
        """Record the agent's task prompt for a run. Called from
        capture.run_command after create_run — keeps the create_run
        signature stable."""
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET task_prompt = ?
                WHERE id = ?
                """,
                (task_prompt, run_id),
            )

    def update_run_lifecycle(
        self,
        run_id: str,
        lifecycle_status: str,
        finalized_at: str | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET lifecycle_status = ?, finalized_at = COALESCE(?, finalized_at)
                WHERE id = ?
                """,
                (lifecycle_status, finalized_at, run_id),
            )

    def add_event(self, run_id: str, event_type: str, timestamp: str, payload: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO events (run_id, event_type, timestamp, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, event_type, timestamp, json.dumps(payload, sort_keys=True)),
            )

    def replace_diagnosis(
        self,
        run_id: str,
        findings: list[dict],
        interventions: list[dict],
    ) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM diagnoses WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM interventions WHERE run_id = ?", (run_id,))
            conn.executemany(
                """
                INSERT INTO diagnoses (run_id, code, title, severity, summary, evidence_json, source)
                VALUES (:run_id, :code, :title, :severity, :summary, :evidence_json, 'rule')
                """,
                findings,
            )
            conn.executemany(
                """
                INSERT INTO interventions (run_id, type, title, target, content, scope, source)
                VALUES (:run_id, :type, :title, :target, :content, :scope, 'rule')
                """,
                interventions,
            )

    def list_runs(self) -> list[RunRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms,
                       summary, task_prompt, client_name, lifecycle_status, finalized_at
                FROM runs
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms,
                       summary, task_prompt, client_name, lifecycle_status, finalized_at
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return RunRecord(**dict(row)) if row else None

    def get_events(self, run_id: str) -> list[EventRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, event_type, timestamp, payload_json
                FROM events
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return [EventRecord(**dict(row)) for row in rows]

    def add_transcript_events(
        self,
        run_id: str,
        events: list[TranscriptEvent],
    ) -> None:
        if not events:
            return
        with self.connection() as conn:
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
                        # Always use the method parameter run_id, never
                        # event.run_id. If a caller builds events with a
                        # stale or mismatched run_id field, the authoritative
                        # answer is the run_id they just passed to this call.
                        "run_id": run_id,
                        "sequence": event.sequence,
                        "kind": event.kind,
                        "tool_name": event.tool_name,
                        "target": event.target,
                        "inputs_summary": event.inputs_summary,
                        "output_excerpt": event.output_excerpt,
                        "status": event.status,
                        "source": event.source,
                        "timestamp": event.timestamp,
                        "raw_ref": event.raw_ref,
                    }
                    for event in events
                ],
            )

    def get_transcript_events(
        self,
        run_id: str,
        kind: str | None = None,
    ) -> list[TranscriptEventRow]:
        with self.connection() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT id, run_id, sequence, kind, tool_name, target,
                           inputs_summary, output_excerpt, status, source, timestamp, raw_ref
                    FROM transcript_events
                    WHERE run_id = ?
                    ORDER BY sequence ASC
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, run_id, sequence, kind, tool_name, target,
                           inputs_summary, output_excerpt, status, source, timestamp, raw_ref
                    FROM transcript_events
                    WHERE run_id = ? AND kind = ?
                    ORDER BY sequence ASC
                    """,
                    (run_id, kind),
                ).fetchall()
        return [TranscriptEventRow(**dict(row)) for row in rows]

    def list_previous_runs(self, cwd: str, before_created_at: str, limit: int = 10) -> list[RunRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms,
                       summary, task_prompt, client_name, lifecycle_status, finalized_at
                FROM runs
                WHERE cwd = ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (cwd, before_created_at, limit),
            ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def get_diagnoses(self, run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT code, title, severity, summary, evidence_json, source
                FROM diagnoses
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return rows

    def get_interventions(self, run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT type, title, target, content, scope, source
                FROM interventions
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return rows

    def replace_llm_diagnosis(
        self,
        run_id: str,
        findings_rows: list[dict],
        interventions_rows: list[dict],
        rule_codes_to_remove: list[str] | None = None,
    ) -> None:
        """Replace only the LLM-sourced findings and interventions for a run.

        Unlike replace_diagnosis (which replaces everything for the run),
        this method only deletes rows with source='llm' before inserting new
        ones. Rule-based findings/interventions from a prior analyze_run pass
        are preserved untouched, unless their code appears in
        rule_codes_to_remove (used when the LLM confirmed/rejected a rule
        finding and the LLM version supersedes it).
        """
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM diagnoses WHERE run_id = ? AND source = 'llm'",
                (run_id,),
            )
            conn.execute(
                "DELETE FROM interventions WHERE run_id = ? AND source = 'llm'",
                (run_id,),
            )
            if rule_codes_to_remove:
                placeholders = ",".join("?" * len(rule_codes_to_remove))
                conn.execute(
                    f"DELETE FROM diagnoses WHERE run_id = ? AND source = 'rule' AND code IN ({placeholders})",
                    (run_id, *rule_codes_to_remove),
                )
            # When LLM interventions are being inserted, also delete rule-sourced
            # interventions to ensure LLM interventions supersede them
            if interventions_rows:
                conn.execute(
                    "DELETE FROM interventions WHERE run_id = ? AND source = 'rule'",
                    (run_id,),
                )
            if findings_rows:
                conn.executemany(
                    """
                    INSERT INTO diagnoses (
                        run_id, code, title, severity, summary, evidence_json, source
                    )
                    VALUES (
                        :run_id, :code, :title, :severity, :summary, :evidence_json, :source
                    )
                    """,
                    findings_rows,
                )
            if interventions_rows:
                conn.executemany(
                    """
                    INSERT INTO interventions (
                        run_id, type, title, target, content, scope, source
                    )
                    VALUES (
                        :run_id, :type, :title, :target, :content, :scope, :source
                    )
                    """,
                    interventions_rows,
                )

    def record_llm_generation(
        self,
        run_id: str,
        kind: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        estimated_cost_usd: float,
        status: str,
        error_message: str | None,
        created_at: str,
        raw_response_excerpt: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_generations (
                    run_id, kind, provider, model,
                    input_tokens, output_tokens, duration_ms,
                    estimated_cost_usd, status, error_message,
                    created_at, raw_response_excerpt
                )
                VALUES (
                    :run_id, :kind, :provider, :model,
                    :input_tokens, :output_tokens, :duration_ms,
                    :estimated_cost_usd, :status, :error_message,
                    :created_at, :raw_response_excerpt
                )
                """,
                {
                    "run_id": run_id,
                    "kind": kind,
                    "provider": provider,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "duration_ms": duration_ms,
                    "estimated_cost_usd": estimated_cost_usd,
                    "status": status,
                    "error_message": error_message,
                    "created_at": created_at,
                    "raw_response_excerpt": raw_response_excerpt,
                },
            )

    def get_llm_generations(self, run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, kind, provider, model,
                       input_tokens, output_tokens, duration_ms,
                       estimated_cost_usd, status, error_message,
                       created_at, raw_response_excerpt
                FROM llm_generations
                WHERE run_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        return rows

    def next_intervention_version(self, source_run_id: str) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS max_version
                FROM intervention_sets
                WHERE source_run_id = ?
                """,
                (source_run_id,),
            ).fetchone()
        return int(row["max_version"]) + 1 if row else 1

    def save_intervention_set(
        self,
        set_id: str,
        source_run_id: str,
        version: int,
        kind: str,
        created_at: str,
        output_dir: str,
        manifest: dict,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO intervention_sets (
                    id, source_run_id, version, kind, created_at, output_dir, manifest_json, applied_at, superseded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT applied_at FROM intervention_sets WHERE id = ?), NULL),
                        COALESCE((SELECT superseded_at FROM intervention_sets WHERE id = ?), NULL))
                """,
                (
                    set_id,
                    source_run_id,
                    version,
                    kind,
                    created_at,
                    output_dir,
                    json.dumps(manifest, sort_keys=True),
                    set_id,
                    set_id,
                ),
            )

    def mark_intervention_set_applied(self, set_id: str, applied_at: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE intervention_sets
                SET applied_at = ?, superseded_at = NULL
                WHERE id = ?
                """,
                (applied_at, set_id),
            )

    def supersede_intervention_sets(self, set_ids: list[str], superseded_at: str) -> None:
        if not set_ids:
            return
        with self.connection() as conn:
            conn.executemany(
                """
                UPDATE intervention_sets
                SET superseded_at = ?
                WHERE id = ?
                """,
                [(superseded_at, set_id) for set_id in set_ids],
            )

    def get_intervention_set(self, set_id: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, source_run_id, version, kind, created_at, output_dir, manifest_json, applied_at, superseded_at
                FROM intervention_sets
                WHERE id = ?
                """,
                (set_id,),
            ).fetchone()
        return row

    def list_intervention_sets_for_run(self, source_run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, source_run_id, version, kind, created_at, output_dir, manifest_json, applied_at, superseded_at
                FROM intervention_sets
                WHERE source_run_id = ?
                ORDER BY version DESC, created_at DESC
                """,
                (source_run_id,),
            ).fetchall()
        return rows

    def list_active_applied_intervention_sets(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, source_run_id, version, kind, created_at, output_dir, manifest_json, applied_at, superseded_at
                FROM intervention_sets
                WHERE applied_at IS NOT NULL AND superseded_at IS NULL
                ORDER BY applied_at ASC, version ASC
                """
            ).fetchall()
        return rows

    def record_replay_run(
        self,
        source_run_id: str,
        replay_run_id: str,
        intervention_set_id: str,
        created_at: str,
        applied_before_replay: bool,
        comparison: dict,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO replay_runs (
                    source_run_id, replay_run_id, intervention_set_id, created_at, applied_before_replay, comparison_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_run_id,
                    replay_run_id,
                    intervention_set_id,
                    created_at,
                    1 if applied_before_replay else 0,
                    json.dumps(comparison, sort_keys=True),
                ),
            )

    def list_replay_runs_for_source(self, source_run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT rr.source_run_id, rr.replay_run_id, rr.intervention_set_id, rr.created_at,
                       rr.applied_before_replay, rr.comparison_json,
                       r.status AS replay_status, r.exit_code AS replay_exit_code, r.summary AS replay_summary,
                       i.manifest_json AS intervention_manifest_json
                FROM replay_runs rr
                JOIN runs r ON r.id = rr.replay_run_id
                JOIN intervention_sets i ON i.id = rr.intervention_set_id
                WHERE rr.source_run_id = ?
                ORDER BY rr.created_at DESC
                """,
                (source_run_id,),
            ).fetchall()
        return rows

    def get_replay_source_for_run(self, replay_run_id: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT rr.source_run_id, rr.replay_run_id, rr.intervention_set_id, rr.created_at,
                       rr.applied_before_replay, rr.comparison_json,
                       i.manifest_json AS intervention_manifest_json
                FROM replay_runs rr
                JOIN intervention_sets i ON i.id = rr.intervention_set_id
                WHERE rr.replay_run_id = ?
                ORDER BY rr.created_at DESC
                LIMIT 1
                """,
                (replay_run_id,),
            ).fetchone()
        return row

    def list_all_replay_runs(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT rr.source_run_id, rr.replay_run_id, rr.intervention_set_id, rr.created_at,
                       rr.applied_before_replay, rr.comparison_json,
                       r.status AS replay_status, r.exit_code AS replay_exit_code, r.summary AS replay_summary,
                       i.manifest_json AS intervention_manifest_json
                FROM replay_runs rr
                JOIN runs r ON r.id = rr.replay_run_id
                JOIN intervention_sets i ON i.id = rr.intervention_set_id
                ORDER BY rr.created_at DESC
                """
            ).fetchall()
        return rows

    def create_pending_action(
        self,
        run_id: str,
        action_type: str,
        title: str,
        payload: dict,
        created_at: str,
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO pending_actions (
                    run_id, action_type, title, payload_json, status, created_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (run_id, action_type, title, json.dumps(payload, sort_keys=True), created_at),
            )
            return int(cursor.lastrowid)

    def list_pending_actions(
        self,
        run_id: str,
        status: str | None = None,
    ) -> list[PendingActionRecord]:
        with self.connection() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT id, run_id, action_type, title, payload_json, status, created_at,
                           approved_at, executed_at, result_json
                    FROM pending_actions
                    WHERE run_id = ?
                    ORDER BY id ASC
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, run_id, action_type, title, payload_json, status, created_at,
                           approved_at, executed_at, result_json
                    FROM pending_actions
                    WHERE run_id = ? AND status = ?
                    ORDER BY id ASC
                    """,
                    (run_id, status),
                ).fetchall()
        return [PendingActionRecord(**dict(row)) for row in rows]

    def get_pending_action(self, action_id: int) -> PendingActionRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, run_id, action_type, title, payload_json, status, created_at,
                       approved_at, executed_at, result_json
                FROM pending_actions
                WHERE id = ?
                """,
                (action_id,),
            ).fetchone()
        return PendingActionRecord(**dict(row)) if row else None

    def approve_pending_action(self, action_id: int, approved_at: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE pending_actions
                SET status = 'approved', approved_at = ?
                WHERE id = ?
                """,
                (approved_at, action_id),
            )

    def complete_pending_action(self, action_id: int, status: str, executed_at: str, result: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE pending_actions
                SET status = ?, executed_at = ?, result_json = ?
                WHERE id = ?
                """,
                (status, executed_at, json.dumps(result, sort_keys=True), action_id),
            )

    def create_memory(
        self,
        kind: str,
        title: str,
        summary: str,
        content: str,
        source_run_id: str | None,
        confidence: float,
        scope: str,
        created_at: str,
        links: list[tuple[str, str]] | None = None,
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories (
                    kind, title, summary, content, source_run_id, confidence, scope, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, title, summary, content, source_run_id, confidence, scope, created_at),
            )
            memory_id = int(cursor.lastrowid)
            if links:
                conn.executemany(
                    """
                    INSERT INTO memory_links (memory_id, link_type, link_value)
                    VALUES (?, ?, ?)
                    """,
                    [(memory_id, link_type, link_value) for link_type, link_value in links],
                )
            return memory_id

    def find_memory_by_title(self, title: str, scope: str = "repo") -> MemoryRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, kind, title, summary, content, source_run_id, confidence, scope, created_at
                FROM memories
                WHERE title = ? AND scope = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (title, scope),
            ).fetchone()
        return MemoryRecord(**dict(row)) if row else None

    def list_memories(self, scope: str | None = None, limit: int = 50) -> list[MemoryRecord]:
        with self.connection() as conn:
            if scope is None:
                rows = conn.execute(
                    """
                    SELECT id, kind, title, summary, content, source_run_id, confidence, scope, created_at
                    FROM memories
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, kind, title, summary, content, source_run_id, confidence, scope, created_at
                    FROM memories
                    WHERE scope = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (scope, limit),
                ).fetchall()
        return [MemoryRecord(**dict(row)) for row in rows]

    def record_memory_hit(self, run_id: str, memory_id: int, reason: str, score: float) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO memory_hits (run_id, memory_id, reason, score)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, memory_id, reason, score),
            )

    def list_memory_hits(self, run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT mh.run_id, mh.memory_id, mh.reason, mh.score,
                       m.kind, m.title, m.summary, m.content, m.scope
                FROM memory_hits mh
                JOIN memories m ON m.id = mh.memory_id
                WHERE mh.run_id = ?
                ORDER BY mh.score DESC, mh.id ASC
                """,
                (run_id,),
            ).fetchall()
        return rows

    def save_compressed_artifact(
        self,
        run_id: str,
        artifact_kind: str,
        artifact_name: str,
        original_text: str,
        compressed_text: str,
        strategy: str,
        preserved_count: int,
        created_at: str,
        fallback_reason: str | None = None,
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO compressed_artifacts (
                    run_id, artifact_kind, artifact_name, original_text, compressed_text, strategy,
                    original_size, compressed_size, preserved_count, fallback_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    artifact_kind,
                    artifact_name,
                    original_text,
                    compressed_text,
                    strategy,
                    len(original_text),
                    len(compressed_text),
                    preserved_count,
                    fallback_reason,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_compressed_artifacts(self, run_id: str) -> list[CompressionArtifactRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, artifact_kind, artifact_name, original_text, compressed_text,
                       strategy, original_size, compressed_size, preserved_count,
                       fallback_reason, created_at
                FROM compressed_artifacts
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return [CompressionArtifactRecord(**dict(row)) for row in rows]

    def run_artifact_dir(self, run_id: str) -> Path:
        path = self.paths.artifacts_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path
