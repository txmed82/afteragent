from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import AppPaths
from .models import EventRecord, RunRecord, TranscriptEventRow
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
                    run_id TEXT NOT NULL REFERENCES runs(id),
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
                """
            )
            self._ensure_column(conn, "interventions", "scope", "TEXT NOT NULL DEFAULT 'pr'")

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
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, command, cwd, status, exit_code, created_at, summary)
                VALUES (?, ?, ?, 'running', NULL, ?, ?)
                """,
                (run_id, command, cwd, created_at, summary),
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
                INSERT INTO diagnoses (run_id, code, title, severity, summary, evidence_json)
                VALUES (:run_id, :code, :title, :severity, :summary, :evidence_json)
                """,
                findings,
            )
            conn.executemany(
                """
                INSERT INTO interventions (run_id, type, title, target, content, scope)
                VALUES (:run_id, :type, :title, :target, :content, :scope)
                """,
                interventions,
            )

    def list_runs(self) -> list[RunRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms, summary
                FROM runs
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [RunRecord(**dict(row)) for row in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms, summary
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
                        "run_id": event.run_id,
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
                SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms, summary
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
                SELECT code, title, severity, summary, evidence_json
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
                SELECT type, title, target, content, scope
                FROM interventions
                WHERE run_id = ?
                ORDER BY id ASC
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

    def run_artifact_dir(self, run_id: str) -> Path:
        path = self.paths.artifacts_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path
