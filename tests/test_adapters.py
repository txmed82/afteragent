import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteragent.adapters import (
    ClaudeCodeAdapter,
    OpenClawAdapter,
    RunnerAdapter,
    ShellAdapter,
    claude_project_slug,
    find_candidate_jsonl,
    select_runner_adapter,
)
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


def test_claude_project_slug_replaces_slashes_and_spaces_with_dashes():
    cwd = Path("/Users/colin/Documents/Google Drive/Business/Public Projects/AfterAgent")
    slug = claude_project_slug(cwd)
    assert slug == "-Users-colin-Documents-Google-Drive-Business-Public-Projects-AfterAgent"


def test_claude_project_slug_simple_path():
    cwd = Path("/home/user/code/repo")
    slug = claude_project_slug(cwd)
    assert slug == "-home-user-code-repo"


def test_claude_pre_launch_snapshot_records_existing_jsonls(tmp_path: Path, monkeypatch):
    # Redirect HOME to tmp_path so we don't touch real ~/.claude.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()
    slug = claude_project_slug(repo)
    project_dir = fake_home / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)
    (project_dir / "existing.jsonl").write_text('{"type":"ping"}\n')

    adapter = ClaudeCodeAdapter()
    state = adapter.pre_launch_snapshot(repo)

    assert state["claude_project_dir"] == project_dir
    assert len(state["pre_jsonl_files"]) == 1
    assert project_dir / "existing.jsonl" in state["pre_jsonl_files"]
    assert "launched_at" in state
    assert isinstance(state["launched_at"], float)


def test_claude_pre_launch_snapshot_handles_missing_project_dir(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "nonexistent"
    repo.mkdir()

    adapter = ClaudeCodeAdapter()
    state = adapter.pre_launch_snapshot(repo)

    # Directory doesn't exist — snapshot still returns a usable dict.
    assert state["pre_jsonl_files"] == {}
    assert "launched_at" in state


def test_find_candidate_jsonl_picks_new_file(tmp_path: Path):
    existing = tmp_path / "existing.jsonl"
    existing.write_text("old\n")
    launched_at = time.time()
    time.sleep(0.01)
    new_file = tmp_path / "new.jsonl"
    new_file.write_text("fresh\n")
    exit_time = time.time()

    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={existing: existing.stat().st_mtime},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    assert chosen == new_file
    assert ambiguous is False


def test_find_candidate_jsonl_picks_modified_file(tmp_path: Path):
    # --continue case: pre-existing file was appended to.
    existing = tmp_path / "existing.jsonl"
    existing.write_text("old\n")
    pre_mtime = existing.stat().st_mtime
    launched_at = time.time()
    time.sleep(0.05)
    existing.write_text("old\nappended\n")  # bump mtime
    exit_time = time.time()

    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={existing: pre_mtime},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    assert chosen == existing
    assert ambiguous is False


def test_find_candidate_jsonl_returns_none_for_zero_candidates(tmp_path: Path):
    launched_at = time.time()
    exit_time = launched_at + 1.0
    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    assert chosen is None
    assert ambiguous is False


def test_find_candidate_jsonl_picks_closest_to_exit_when_ambiguous(tmp_path: Path):
    launched_at = time.time()
    a = tmp_path / "a.jsonl"
    a.write_text("")
    time.sleep(0.05)
    b = tmp_path / "b.jsonl"
    b.write_text("")  # b's mtime is later than a's but still within window
    exit_time = time.time() + 0.1

    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    # Both are candidates (both post-launch). Expect the one closest to exit.
    assert chosen == b
    assert ambiguous is True


def test_claude_code_adapter_parse_transcript_end_to_end(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()
    slug = claude_project_slug(repo)
    project_dir = fake_home / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)

    adapter = ClaudeCodeAdapter()
    pre_state = adapter.pre_launch_snapshot(repo)

    # Simulate Claude Code writing a new JSONL file mid-run.
    fixture = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "simple_edit_run.jsonl"
    session_jsonl = project_dir / "sess-simple.jsonl"
    session_jsonl.write_text(fixture.read_text())

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    events = adapter.parse_transcript(
        run_id="r1",
        artifact_dir=artifact_dir,
        stdout="",
        stderr="",
        pre_launch_state=pre_state,
    )

    assert len(events) >= 1
    assert all(e.source == "claude_code_jsonl" for e in events)
    # The raw JSONL was copied into the run's transcripts dir.
    copied = artifact_dir / "transcripts" / "session.jsonl"
    assert copied.exists()
    assert copied.read_text() == fixture.read_text()


def test_claude_code_adapter_parse_transcript_falls_back_when_no_jsonl(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()

    adapter = ClaudeCodeAdapter()
    pre_state = adapter.pre_launch_snapshot(repo)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    stdout = "pytest tests/\nFAILED: boom\n"
    events = adapter.parse_transcript(
        run_id="r1",
        artifact_dir=artifact_dir,
        stdout=stdout,
        stderr="",
        pre_launch_state=pre_state,
    )

    # No JSONL found → at least one parse_error + generic stdout events.
    assert any(e.kind == "parse_error" for e in events)
    assert any(e.source == "stdout_heuristic" for e in events)
