import io
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from afteragent.adapters import ClaudeCodeAdapter, CodexAdapter, RunnerAdapter, claude_project_slug
from afteragent.capture import run_command, validate_github_pr
from afteragent.config import AppPaths, resolve_paths
from afteragent.store import Store
from afteragent.transcripts import KIND_FILE_READ, SOURCE_CLAUDE_CODE_JSONL, TranscriptEvent


class CaptureTests(unittest.TestCase):
    def test_run_command_streams_and_persists_output(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            store = Store(
                make_paths(root)
            )
            stdout_stream = io.StringIO()
            stderr_stream = io.StringIO()
            command = [
                "python3",
                "-c",
                (
                    "import sys; "
                    "print('streamed stdout', flush=True); "
                    "print('streamed stderr', file=sys.stderr, flush=True)"
                ),
            ]

            with patch("sys.stdout", stdout_stream), patch("sys.stderr", stderr_stream):
                result = run_command(store, command, Path(tmpdir), stream_output=True)

            run_id = str(result["run_id"])
            artifact_dir = store.run_artifact_dir(run_id)
            self.assertEqual(int(result["exit_code"]), 0)
            self.assertIn("streamed stdout", stdout_stream.getvalue())
            self.assertIn("streamed stderr", stderr_stream.getvalue())
            self.assertIn("streamed stdout", (artifact_dir / "stdout.log").read_text())
            self.assertIn("streamed stderr", (artifact_dir / "stderr.log").read_text())

    def test_validate_github_pr_creates_run_from_snapshot(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            store = Store(
                make_paths(root)
            )
            snapshot = {
                "repo": "octo/repo",
                "pr_number": 17,
                "review_summary": {"unresolved_thread_count": 1},
                "checks": [{"name": "pytest", "bucket": "fail"}],
                "ci_runs": [],
            }
            with patch("afteragent.capture.capture_github_context", return_value=snapshot):
                result = validate_github_pr(store, "octo/repo", 17, Path(tmpdir))

            run = store.get_run(str(result["run_id"]))
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "failed")
            self.assertIn("failing_checks=1", run.summary or "")

    def test_run_command_records_legacy_pattern_events_from_adapter(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            store = Store(make_paths(root))
            command = [
                "python3",
                "-c",
                (
                    "print('tool call: apply_patch'); "
                    "print('edited src/app.py'); "
                    "print('retrying 2')"
                ),
            ]

            result = run_command(
                store,
                command,
                Path(tmpdir),
                stream_output=False,
                adapter=CodexAdapter(),
            )

            events = store.get_events(str(result["run_id"]))
            event_types = [event.event_type for event in events]
            self.assertIn("tool.called", event_types)
            self.assertIn("file.edited", event_types)
            self.assertIn("retry.detected", event_types)

    def test_run_command_marks_missing_binary_as_failed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            store = Store(make_paths(root))

            result = run_command(
                store,
                ["definitely-not-a-real-binary-afteragent"],
                Path(tmpdir),
                stream_output=False,
            )

            run = store.get_run(str(result["run_id"]))
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.exit_code, 127)
            self.assertIn("Spawn failed", run.summary or "")
            events = store.get_events(run.id)
            self.assertIn("process.spawn_failed", [event.event_type for event in events])
            stderr_text = (store.run_artifact_dir(run.id) / "stderr.log").read_text()
            self.assertIn("FileNotFoundError", stderr_text)

    def test_run_command_marks_permission_error_as_failed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            store = Store(make_paths(root))
            script_path = Path(tmpdir) / "noexec.sh"
            script_path.write_text("#!/bin/sh\necho blocked\n")
            script_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

            result = run_command(
                store,
                [str(script_path)],
                Path(tmpdir),
                stream_output=False,
            )

            run = store.get_run(str(result["run_id"]))
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.exit_code, 126)
            events = store.get_events(run.id)
            self.assertIn("process.spawn_failed", [event.event_type for event in events])
            self.assertIn("run.finished", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()


def test_run_command_calls_pre_launch_snapshot(tmp_path: Path):
    calls = []

    class Adapter(RunnerAdapter):
        name = "simple"

        def pre_launch_snapshot(self, cwd):
            calls.append(cwd)
            return {}

    store = Store(resolve_paths(tmp_path))
    run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=Adapter(),
    )
    assert len(calls) == 1
    assert calls[0] == tmp_path


def test_run_command_writes_transcript_events_from_adapter(tmp_path: Path):
    class StubAdapter(RunnerAdapter):
        name = "stub"

        def pre_launch_snapshot(self, cwd):
            return {"hello": "world"}

        def parse_transcript(self, run_id, artifact_dir, stdout, stderr, pre_launch_state):
            assert pre_launch_state == {"hello": "world"}
            return [
                TranscriptEvent(
                    run_id=run_id,
                    sequence=0,
                    kind=KIND_FILE_READ,
                    tool_name="Read",
                    target="/repo/README.md",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    raw_ref="line:1",
                    inputs_summary="",
                    output_excerpt="",
                    status="success",
                    timestamp="2026-04-10T12:00:00Z",
                ),
                TranscriptEvent(
                    run_id=run_id,
                    sequence=1,
                    kind=KIND_FILE_READ,
                    tool_name="Read",
                    target="/repo/a.py",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    raw_ref="line:2",
                    inputs_summary="",
                    output_excerpt="",
                    status="success",
                    timestamp="2026-04-10T12:00:01Z",
                ),
            ]

    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=StubAdapter(),
    )

    rows = store.get_transcript_events(result["run_id"])
    assert len(rows) == 2
    assert rows[0].target == "/repo/README.md"
    assert rows[0].sequence == 0
    assert rows[1].target == "/repo/a.py"
    assert rows[1].sequence == 1


def test_run_command_precreates_transcripts_artifact_subdir(tmp_path: Path):
    subdir_seen = []

    class Adapter(RunnerAdapter):
        name = "check"

        def parse_transcript(self, run_id, artifact_dir, stdout, stderr, pre_launch_state):
            subdir_seen.append(artifact_dir / "transcripts")
            return []

    store = Store(resolve_paths(tmp_path))
    run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=Adapter(),
    )
    assert len(subdir_seen) == 1
    # The transcripts subdir must have been pre-created so the adapter could
    # have written into it. The parser returned [] so nothing was actually
    # written, but the directory must exist.
    assert subdir_seen[0].exists()


def test_capture_full_pipeline_with_real_claude_code_adapter(tmp_path: Path, monkeypatch):
    """Exercises snapshot → subprocess → parse → store with a real ClaudeCodeAdapter.

    Uses a fake ~/.claude/projects/<slug>/ layout. The subprocess writes the
    fixture JSONL into the project dir (simulating Claude Code writing its
    transcript during the run), and monkeypatch HOME so the adapter looks at
    our fake dir.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()
    slug = claude_project_slug(repo)
    project_dir = fake_home / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)

    # Path the subprocess will write the transcript to.
    fixture = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "simple_edit_run.jsonl"
    session_path = project_dir / "sess-simple.jsonl"

    # The subprocess copies the fixture JSONL into the project dir, simulating
    # Claude Code writing its transcript during the run.  Because the file does
    # not exist when pre_launch_snapshot runs, find_candidate_jsonl treats it
    # as a brand-new file (not in pre_jsonl_files) and always picks it up.
    write_cmd = (
        f"import shutil; shutil.copy({str(fixture)!r}, {str(session_path)!r})"
    )

    adapter = ClaudeCodeAdapter()
    store = Store(resolve_paths(tmp_path / "afteragent-root"))

    result = run_command(
        store=store,
        command=["python3", "-c", write_cmd],
        cwd=repo,
        adapter=adapter,
    )
    run_id = result["run_id"]

    rows = store.get_transcript_events(run_id)
    assert len(rows) > 0
    # All non-parse-error events should be tagged with the Claude Code source.
    non_errors = [r for r in rows if r.kind != "parse_error"]
    assert all(r.source == "claude_code_jsonl" for r in non_errors)

    # Kind coverage: we expect at least one file_read and one file_edit
    # from the fixture.
    kinds = {r.kind for r in rows}
    assert "file_read" in kinds
    assert "file_edit" in kinds

    # Raw transcript was copied into the artifact dir.
    artifacts_root = store.paths.artifacts_dir / run_id / "transcripts"
    assert (artifacts_root / "session.jsonl").exists()


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        db_path=root / "afteragent.sqlite3",
        artifacts_dir=root / "artifacts",
        exports_dir=root / "exports",
        applied_dir=root / "applied",
        replays_dir=root / "replays",
        config_path=root / "config.toml",
    )


def test_run_command_auto_parses_task_prompt_from_claude_command(tmp_path: Path):
    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=tmp_path,
        adapter=ClaudeCodeAdapter(),
    )
    # The adapter-based parse returns None for a python3 command, so the
    # fallback is shlex.join(command). Verify that's what landed.
    run = store.get_run(result["run_id"])
    assert run is not None
    assert run.task_prompt is not None
    # Full-command fallback for a command with no recognizable prompt shape.
    assert "python3" in run.task_prompt


def test_run_command_explicit_task_kwarg_wins_over_adapter_parse(tmp_path: Path):
    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=tmp_path,
        adapter=ClaudeCodeAdapter(),
        task_prompt="explicit override task",
    )
    run = store.get_run(result["run_id"])
    assert run is not None
    assert run.task_prompt == "explicit override task"


def test_run_command_falls_back_to_shlex_join_when_adapter_returns_none(tmp_path: Path):
    """With ShellAdapter (base parse_task_prompt returns None), the task
    prompt falls back to shlex.join(command)."""
    from afteragent.adapters import ShellAdapter

    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=tmp_path,
        adapter=ShellAdapter(),
    )
    run = store.get_run(result["run_id"])
    assert run is not None
    # shlex.join on the command list.
    assert "python3" in run.task_prompt
    assert "print" in run.task_prompt
