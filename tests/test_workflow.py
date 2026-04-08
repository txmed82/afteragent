import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteraction.config import AppPaths
from afteraction.store import Store
from afteraction.workflow import apply_interventions, attempt_repair, export_interventions, replay_run


class WorkflowTests(unittest.TestCase):
    def test_export_and_apply_interventions_write_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")

            export_manifest = export_interventions(store, "run123", Path(tmpdir))
            self.assertEqual(export_manifest["version"], 1)
            self.assertTrue(export_manifest["intervention_set_id"])
            self.assertTrue(Path(export_manifest["manifest_path"]).exists())
            self.assertTrue(Path(export_manifest["exports"]["task_prompt"]).exists())
            self.assertTrue(Path(export_manifest["exports"]["runner_policy"]).exists())
            self.assertIn("instruction_patches", export_manifest["exports"])

            apply_manifest = apply_interventions(store, "run123", Path(tmpdir))
            agents_path = Path(tmpdir) / "AGENTS.md"
            self.assertTrue(agents_path.exists())
            self.assertIn("AfterAction Interventions", agents_path.read_text())
            self.assertTrue(any("AGENTS.md" in path for path in apply_manifest["applied_paths"]))
            rows = store.list_intervention_sets_for_run("run123")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "applied")
            self.assertIsNotNone(rows[0]["applied_at"])

    def test_apply_interventions_targets_existing_claude_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")
            claude_path = Path(tmpdir) / "CLAUDE.md"
            claude_path.write_text("# Claude\n")

            apply_manifest = apply_interventions(store, "run123", Path(tmpdir))

            self.assertTrue(claude_path.exists())
            self.assertIn("AfterAction Interventions", claude_path.read_text())
            self.assertFalse((Path(tmpdir) / "AGENTS.md").exists())
            self.assertTrue(any("CLAUDE.md" in path for path in apply_manifest["applied_paths"]))

    def test_replay_run_injects_context_env(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")

            result = replay_run(
                store,
                "run123",
                cwd=Path(tmpdir),
                command=[
                    "python3",
                    "-c",
                    (
                        "import os; "
                        "print(os.environ['AFTERACTION_SOURCE_RUN']); "
                        "print(os.environ['AFTERACTION_TASK_PROMPT_PATH']); "
                        "print(os.environ['AFTERACTION_INTERVENTION_MANIFEST_PATH']); "
                        "print(os.environ['AFTERACTION_INSTRUCTION_TARGETS']); "
                        "print(os.environ['AFTERACTION_RUNNER_ADAPTER'])"
                    ),
                ],
                stream_output=False,
            )
            artifact_dir = store.run_artifact_dir(str(result["run_id"]))
            stdout_text = (artifact_dir / "stdout.log").read_text()
            self.assertIn("run123", stdout_text)
            self.assertIn(".afteraction/replays/run123", stdout_text)
            self.assertIn("AGENTS.md", stdout_text)
            self.assertIn("shell", stdout_text)
            replay_rows = store.list_replay_runs_for_source("run123")
            self.assertEqual(len(replay_rows), 1)
            comparison = json.loads(replay_rows[0]["comparison_json"])
            self.assertEqual(comparison["source_status"], "failed")
            self.assertEqual(comparison["replay_status"], "passed")
            self.assertEqual(comparison["verdict"], "improved")
            self.assertGreater(comparison["score"], 0)

    def test_attempt_repair_applies_and_replays_from_existing_run(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")

            result = attempt_repair(
                store,
                cwd=Path(tmpdir),
                source_run_id="run123",
                command=["python3", "-c", "print('repair')"],
                stream_output=False,
            )

            self.assertEqual(result["source_run_id"], "run123")
            self.assertTrue(result["applied_manifest"]["applied_paths"])
            self.assertEqual(result["runner_adapter"], "shell")
            self.assertEqual(result["comparison"]["verdict"], "improved")

    def test_attempt_repair_can_force_runner_preset(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")

            result = attempt_repair(
                store,
                cwd=Path(tmpdir),
                source_run_id="run123",
                command=["python3", "-c", "print('repair')"],
                stream_output=False,
                runner="claude-code",
            )

            self.assertEqual(result["runner_adapter"], "claude-code")
            self.assertIn("CLAUDE.md", result["applied_manifest"]["instruction_targets"])

    def test_apply_interventions_supersedes_prior_pr_scoped_sets(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")
            seed_review_instruction_run(store, "run456")

            apply_interventions(store, "run123", Path(tmpdir))
            apply_interventions(store, "run456", Path(tmpdir))

            agents_text = (Path(tmpdir) / "AGENTS.md").read_text()
            self.assertIn("Set v1 · run `run456`", agents_text)
            self.assertIn("gather unresolved review comments", agents_text)
            self.assertNotIn("Set v1 · run `run123`", agents_text)

            rows = store.list_intervention_sets_for_run("run123")
            self.assertIsNotNone(rows[0]["superseded_at"])


def seed_diagnosed_run(store: Store, run_id: str) -> None:
    store.create_run(run_id, "python3 agent.py", "/repo", "2026-04-08T12:00:00Z", summary="seed")
    store.finish_run(run_id, "failed", 1, "2026-04-08T12:02:00Z", 1000, summary="seed")
    artifact_dir = store.run_artifact_dir(run_id)
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("AssertionError: failed\n")
    (artifact_dir / "git_diff_after.patch").write_text(
        "\n".join(
            [
                "diff --git a/src/a.py b/src/a.py",
                "--- a/src/a.py",
                "+++ b/src/a.py",
                "@@ -1 +1 @@",
                "+change",
            ]
        )
    )
    (artifact_dir / "github_context.json").write_text(
        json.dumps(
            {
                "repo": "octo/repo",
                "pr_number": 19,
                "pr_changed_files": ["src/a.py"],
                "review_summary": {
                    "issue_comment_count": 0,
                    "review_count": 1,
                    "thread_count": 1,
                    "unresolved_thread_count": 1,
                },
                "review_threads": [
                    {
                        "id": "thread-1",
                        "is_resolved": False,
                        "path": "src/b.py",
                        "line": 3,
                        "latest_comment_at": "2026-04-08T11:00:00Z",
                        "comments": [],
                    }
                ],
                "checks": [{"name": "pytest", "bucket": "fail", "state": "FAILURE"}],
                "ci_runs": [
                    {"failed_log_excerpt": ["src/a.py:3: AssertionError", "FAILED tests/test_a.py::test_it"]}
                ],
            }
        )
    )


def seed_review_instruction_run(store: Store, run_id: str) -> None:
    store.create_run(run_id, "python3 agent.py", "/repo", "2026-04-08T13:00:00Z", summary="seed")
    store.finish_run(run_id, "failed", 1, "2026-04-08T13:02:00Z", 1000, summary="seed")
    artifact_dir = store.run_artifact_dir(run_id)
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("AssertionError: failed\n")
    (artifact_dir / "git_diff_after.patch").write_text(
        "\n".join(
            [
                "diff --git a/src/a.py b/src/a.py",
                "--- a/src/a.py",
                "+++ b/src/a.py",
                "@@ -1 +1 @@",
                "+change",
            ]
        )
    )
    (artifact_dir / "github_context.json").write_text(
        json.dumps(
            {
                "repo": "octo/repo",
                "pr_number": 20,
                "pr_changed_files": ["src/a.py"],
                "review_summary": {
                    "issue_comment_count": 0,
                    "review_count": 1,
                    "thread_count": 1,
                    "unresolved_thread_count": 1,
                },
                "review_threads": [
                    {
                        "id": "thread-2",
                        "is_resolved": False,
                        "path": "src/review.py",
                        "line": 4,
                        "latest_comment_at": "2026-04-08T12:00:00Z",
                        "comments": [],
                    }
                ],
                "checks": [],
                "ci_runs": [],
            }
        )
    )


def make_paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        db_path=root / "afteraction.sqlite3",
        artifacts_dir=root / "artifacts",
        exports_dir=root / "exports",
        applied_dir=root / "applied",
        replays_dir=root / "replays",
    )


if __name__ == "__main__":
    unittest.main()
