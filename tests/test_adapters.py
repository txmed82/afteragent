import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteragent.adapters import ClaudeCodeAdapter, OpenClawAdapter, RunnerAdapter, ShellAdapter, select_runner_adapter
from afteragent.transcripts import KIND_TEST_RUN, SOURCE_STDOUT_HEURISTIC


class AdapterTests(unittest.TestCase):
    def test_select_runner_adapter_prefers_claude_for_command(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            adapter = select_runner_adapter(cwd, command=["claude", "run"])

            self.assertEqual(adapter.name, "claude-code")
            self.assertEqual(adapter.instruction_targets(cwd)[0].name, "CLAUDE.md")

    def test_select_runner_adapter_prefers_codex_for_command(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            adapter = select_runner_adapter(cwd, command=["codex", "exec"])

            self.assertEqual(adapter.name, "codex")
            self.assertEqual(adapter.instruction_targets(cwd)[0].name, "AGENTS.md")

    def test_select_runner_adapter_prefers_openclaw_for_command(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            adapter = select_runner_adapter(cwd, command=["openclaw", "run"])

            self.assertEqual(adapter.name, "openclaw")
            self.assertEqual(adapter.instruction_targets(cwd)[0].name, "AGENTS.md")

    def test_shell_adapter_falls_back_for_unknown_runner(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            adapter = select_runner_adapter(cwd, command=["hermes", "run"])

            self.assertEqual(adapter.name, "shell")
            self.assertEqual(adapter.instruction_targets(cwd)[0].name, "AGENTS.md")

    def test_unknown_runner_does_not_get_forced_into_codex_by_agents_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            (cwd / "AGENTS.md").write_text("")

            adapter = select_runner_adapter(cwd, command=["hermes", "run"])

            self.assertEqual(adapter.name, "shell")

    def test_shell_adapter_uses_all_existing_instruction_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            (cwd / "AGENTS.md").write_text("")
            (cwd / "CLAUDE.md").write_text("")

            targets = ShellAdapter().instruction_targets(cwd)

            self.assertEqual([path.name for path in targets], ["AGENTS.md", "CLAUDE.md"])

    def test_openclaw_adapter_parses_transcript_events(self) -> None:
        with TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir)
            (artifact_dir / "openclaw-run.log").write_text(
                "\n".join(
                    [
                        "tool: apply_patch",
                        "edited src/app.py",
                        "retrying 2",
                    ]
                )
            )

            events = OpenClawAdapter().parse_transcript_events("", "", artifact_dir)

            self.assertEqual([item["event_type"] for item in events], ["tool.called", "file.edited", "retry.detected"])


if __name__ == "__main__":
    unittest.main()


def test_base_pre_launch_snapshot_returns_empty_dict(tmp_path: Path):
    adapter = ShellAdapter()
    state = adapter.pre_launch_snapshot(tmp_path)
    assert state == {}


def test_base_parse_transcript_uses_generic_stdout_parser(tmp_path: Path):
    adapter = ShellAdapter()
    stdout = "============================= test session starts ===\nFAILED: boom\n"
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout=stdout,
        stderr="",
        pre_launch_state={},
    )
    assert len(events) >= 1
    assert any(e.kind == KIND_TEST_RUN for e in events)
    assert all(e.source == SOURCE_STDOUT_HEURISTIC for e in events)


def test_base_parse_transcript_never_raises_on_garbage(tmp_path: Path):
    adapter = ShellAdapter()
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout="\x00\x01\x02",
        stderr="",
        pre_launch_state={},
    )
    assert isinstance(events, list)


def test_base_parse_transcript_returns_empty_list_for_empty_input(tmp_path: Path):
    adapter = ShellAdapter()
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout="",
        stderr="",
        pre_launch_state={},
    )
    assert events == []


def test_openclaw_inherits_generic_parser_by_default(tmp_path: Path):
    adapter = OpenClawAdapter()
    stdout = "npm test\npassed 3 tests\n"
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout=stdout,
        stderr="",
        pre_launch_state={},
    )
    assert all(e.source == SOURCE_STDOUT_HEURISTIC for e in events)
