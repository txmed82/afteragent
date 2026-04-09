import io
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from afteragent.adapters import CodexAdapter
from afteragent.capture import run_command, validate_github_pr
from afteragent.config import AppPaths
from afteragent.store import Store


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

    def test_run_command_records_transcript_events_from_adapter(self) -> None:
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


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        db_path=root / "afteragent.sqlite3",
        artifacts_dir=root / "artifacts",
        exports_dir=root / "exports",
        applied_dir=root / "applied",
        replays_dir=root / "replays",
    )
