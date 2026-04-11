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
