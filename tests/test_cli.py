import unittest

from afteragent.cli import normalize_replay_args


class CliTests(unittest.TestCase):
    def test_normalize_replay_args_extracts_misplaced_options(self) -> None:
        summary, apply_interventions, no_stream, command = normalize_replay_args(
            None,
            False,
            False,
            [
                "--summary",
                "live replay",
                "--apply-interventions",
                "--no-stream",
                "--",
                "python3",
                "-c",
                "print('ok')",
            ],
        )

        self.assertEqual(summary, "live replay")
        self.assertTrue(apply_interventions)
        self.assertTrue(no_stream)
        self.assertEqual(command, ["python3", "-c", "print('ok')"])


if __name__ == "__main__":
    unittest.main()


def test_enhance_subcommand_parses_and_dispatches(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())
    store.create_run("test_run_id", "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("test_run_id")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("test_run_id", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    from afteragent.cli import main

    for var in [
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL", "AFTERAGENT_LLM_PROVIDER",
    ]:
        monkeypatch.delenv(var, raising=False)

    exit_code = main(["enhance", "test_run_id"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "No LLM provider configured" in captured.out or "No LLM provider configured" in captured.err


def test_enhance_subcommand_calls_enhancer_when_configured(tmp_path, monkeypatch, capsys):
    from unittest.mock import MagicMock, patch
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())
    store.create_run("test_run_id", "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("test_run_id")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("test_run_id", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    from afteragent.cli import main
    from afteragent.llm.enhancer import EnhanceResult

    stub_client = MagicMock()
    stub_client.name = "anthropic"
    stub_client.model = "claude-sonnet-4-5"

    with patch("afteragent.cli.get_client", return_value=stub_client), \
         patch("afteragent.cli.enhance_diagnosis_with_llm") as mock_enhance:
        mock_enhance.return_value = EnhanceResult(
            status="success",
            findings_count=2,
            interventions_count=1,
            total_input_tokens=1000,
            total_output_tokens=100,
            total_cost_usd=0.005,
            error_messages=[],
        )

        exit_code = main(["enhance", "test_run_id"])

    assert exit_code == 0
    mock_enhance.assert_called_once()
    captured = capsys.readouterr()
    assert "Enhanced run" in captured.out
    assert "test_run_id" in captured.out
    assert "2 finding" in captured.out or "+2 findings" in captured.out
    assert "1 intervention" in captured.out


def test_exec_enhance_flag_present(tmp_path, monkeypatch):
    from afteragent.cli import build_parser
    parser = build_parser()

    args = parser.parse_args(["exec", "--enhance", "--", "echo", "hi"])
    assert getattr(args, "enhance", None) is True

    args = parser.parse_args(["exec", "--no-enhance", "--", "echo", "hi"])
    assert getattr(args, "enhance", None) is False


def test_stats_subcommand_empty_store(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from afteragent.cli import main

    exit_code = main(["stats"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No replays recorded yet." in captured.out
    assert "0 total replays" in captured.out


def test_stats_subcommand_populated_store(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())

    # Seed 5 replays with the same finding code, all resolved.
    for i in range(5):
        source_id = f"src{i}"
        replay_id = f"rep{i}"
        store.create_run(source_id, "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
        store.replace_diagnosis(
            source_id,
            [{
                "run_id": source_id,
                "code": "low_diff_overlap",
                "title": "x",
                "severity": "medium",
                "summary": "x",
                "evidence_json": "[]",
            }],
            [],
        )
        store.create_run(replay_id, "echo replay", str(tmp_path), "2026-04-10T13:00:00Z")
        store.finish_run(replay_id, "passed", 0, "2026-04-10T13:00:01Z", 1000, summary="ok")
        store.save_intervention_set(
            set_id=f"iset{i}",
            source_run_id=source_id,
            version=1,
            kind="export",
            created_at="2026-04-10T13:00:00Z",
            output_dir="/tmp/fake",
            manifest={"interventions": []},
        )
        store.record_replay_run(
            source_run_id=source_id,
            replay_run_id=replay_id,
            intervention_set_id=f"iset{i}",
            created_at="2026-04-10T13:00:00Z",
            applied_before_replay=True,
            comparison={"resolved_findings": ["low_diff_overlap"], "improved": True, "score": 10},
        )

    from afteragent.cli import main
    exit_code = main(["stats"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "5 total replays" in captured.out
    assert "Finding code resolution rates:" in captured.out
    assert "low_diff_overlap" in captured.out
    assert "100%" in captured.out


def test_stats_subcommand_accepts_min_samples_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())

    # Seed 3 replays — below default threshold 5.
    for i in range(3):
        source_id = f"src{i}"
        replay_id = f"rep{i}"
        store.create_run(source_id, "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
        store.replace_diagnosis(
            source_id,
            [{
                "run_id": source_id,
                "code": "code_x",
                "title": "x",
                "severity": "medium",
                "summary": "x",
                "evidence_json": "[]",
            }],
            [],
        )
        store.create_run(replay_id, "echo replay", str(tmp_path), "2026-04-10T13:00:00Z")
        store.finish_run(replay_id, "passed", 0, "2026-04-10T13:00:01Z", 1000, summary="ok")
        store.save_intervention_set(
            set_id=f"iset{i}",
            source_run_id=source_id,
            version=1,
            kind="export",
            created_at="2026-04-10T13:00:00Z",
            output_dir="/tmp/fake",
            manifest={"interventions": []},
        )
        store.record_replay_run(
            source_run_id=source_id,
            replay_run_id=replay_id,
            intervention_set_id=f"iset{i}",
            created_at="2026-04-10T13:00:00Z",
            applied_before_replay=True,
            comparison={"resolved_findings": ["code_x"], "improved": True, "score": 10},
        )

    from afteragent.cli import main

    # With default min_samples=5, metric is below threshold → not shown.
    exit_code = main(["stats"])
    captured_default = capsys.readouterr()
    assert "code_x" not in captured_default.out

    # With --min-samples 3, metric is shown.
    exit_code = main(["stats", "--min-samples", "3"])
    captured_low = capsys.readouterr()
    assert "code_x" in captured_low.out


def test_exec_accepts_task_flag(tmp_path, monkeypatch):
    from afteragent.cli import build_parser
    parser = build_parser()

    args = parser.parse_args([
        "exec", "--task", "deploy to staging", "--",
        "python3", "-c", "print('hi')",
    ])
    assert getattr(args, "task_prompt", None) == "deploy to staging"


def test_exec_populates_task_prompt_from_claude_command_auto(tmp_path, monkeypatch, capsys):
    """When no --task flag is passed, the adapter's parse_task_prompt (or
    the shlex.join fallback) populates the column."""
    monkeypatch.chdir(tmp_path)

    from afteragent.cli import main
    from afteragent.config import resolve_paths
    from afteragent.store import Store

    # Use a no-op command so the test doesn't invoke claude.
    exit_code = main(["exec", "--", "python3", "-c", "print('hi')"])
    assert exit_code == 0

    store = Store(resolve_paths())
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].task_prompt is not None
    assert len(runs[0].task_prompt) > 0
