import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteragent.adapters import OpenClawAdapter, ShellAdapter, select_runner_adapter


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
