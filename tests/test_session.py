from pathlib import Path
from tempfile import TemporaryDirectory

from afteragent.cli import main
from afteragent.config import resolve_paths
from afteragent.session import append_events, approve_actions, finalize_run, start_run
from afteragent.store import Store


def _make_store(tmp_path: Path) -> Store:
    return Store(resolve_paths(tmp_path))


def test_finalize_run_creates_recommendations_and_compression():
    with TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = _make_store(tmp)
        started = start_run(
            store,
            cwd=tmp,
            task_prompt="Improve the React UI and ship a better frontend experience",
            client_name="cursor",
        )
        append_events(
            store,
            started["run_id"],
            [
                {"event_type": "file.read", "target": "src/App.tsx", "tool_name": "Read"},
                {"event_type": "file.edited", "target": "src/App.tsx", "tool_name": "Edit"},
                {"event_type": "command.finished", "target": "pytest -q", "exit_code": 0, "output": "1 passed"},
            ],
        )

        result = finalize_run(store, started["run_id"])

        assert result["status"] == "passed"
        assert any(item["title"] == "frontend-design" for item in result["recommendations"])
        assert any(item["artifact_kind"] == "transcript" for item in result["compression_report"])
        run = store.get_run(started["run_id"])
        assert run is not None
        assert run.lifecycle_status == "finalized"


def test_finalize_run_creates_repo_instruction_pending_action_and_approve_applies_it():
    with TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        store = _make_store(tmp)
        started = start_run(
            store,
            cwd=tmp,
            task_prompt="Fix the bug in src/app.py",
            client_name="codex",
        )
        append_events(
            store,
            started["run_id"],
            [
                {"event_type": "file.read", "target": "src/app.py", "tool_name": "Read"},
                {"event_type": "file.edited", "target": "src/app.py", "tool_name": "Edit"},
                {"event_type": "message", "message": "Done", "role": "assistant"},
            ],
        )

        result = finalize_run(store, started["run_id"])
        repo_actions = [item for item in result["pending_actions"] if item["type"] == "apply_repo_instruction_patch"]
        assert repo_actions

        approval_results = approve_actions(store, started["run_id"], tmp)

        assert approval_results
        assert (tmp / "AGENTS.md").exists()
        assert "AfterAction Interventions" in (tmp / "AGENTS.md").read_text()


def test_cli_finalize_and_approve_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = _make_store(tmp_path)
    started = start_run(
        store,
        cwd=tmp_path,
        task_prompt="Fix the bug in src/app.py",
        client_name="codex",
    )
    append_events(
        store,
        started["run_id"],
        [
            {"event_type": "file.read", "target": "src/app.py", "tool_name": "Read"},
            {"event_type": "file.edited", "target": "src/app.py", "tool_name": "Edit"},
        ],
    )

    exit_code = main(["finalize", started["run_id"]])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "finalized run" in captured.out
    assert "pending actions:" in captured.out

    action = store.list_pending_actions(started["run_id"])[0]
    exit_code = main(["approve", started["run_id"], "--action-id", str(action.id)])
    assert exit_code == 0
    assert (tmp_path / "AGENTS.md").exists()
