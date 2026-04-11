import tempfile
from pathlib import Path

import pytest

from afteragent.config import resolve_paths
from afteragent.models import RunRecord
from afteragent.store import Store


def _make_store(tmp: Path) -> Store:
    return Store(resolve_paths(tmp))


def test_run_record_roundtrips_task_prompt():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        store.create_run("run1", "claude 'fix it'", "/tmp", "2026-04-11T12:00:00Z")
        store.set_run_task_prompt("run1", "fix it")

        run = store.get_run("run1")
        assert isinstance(run, RunRecord)
        assert run.task_prompt == "fix it"


def test_set_run_task_prompt_updates_existing_row():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        store.create_run("run1", "claude 'first'", "/tmp", "2026-04-11T12:00:00Z")
        store.set_run_task_prompt("run1", "first task")
        store.set_run_task_prompt("run1", "revised task")

        run = store.get_run("run1")
        assert run.task_prompt == "revised task"


def test_existing_run_without_task_prompt_returns_none():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Simulate a legacy run: insert row, do not call set_run_task_prompt.
        store.create_run("legacy", "some cmd", "/tmp", "2026-04-11T12:00:00Z")

        run = store.get_run("legacy")
        assert run is not None
        assert run.task_prompt is None


def test_set_run_task_prompt_migration_idempotent_on_existing_db():
    """Constructing Store twice against the same DB must not fail on the
    additive column migration."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store1 = _make_store(tmp)
        store1.create_run("run1", "cmd", "/tmp", "2026-04-11T12:00:00Z")
        store1.set_run_task_prompt("run1", "task")

        # Second construction — _ensure_column must no-op since the column exists.
        store2 = _make_store(tmp)
        run = store2.get_run("run1")
        assert run is not None
        assert run.task_prompt == "task"
