import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteraction.config import AppPaths
from afteraction.store import Store
from afteraction.ui import summarize_effectiveness
from afteraction.workflow import replay_run


class UiTests(unittest.TestCase):
    def test_summarize_effectiveness_rolls_up_replay_scores(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteraction"
            store = Store(make_paths(root))
            seed_diagnosed_run(store, "run123")

            replay_run(
                store,
                "run123",
                cwd=Path(tmpdir),
                command=["python3", "-c", "print('ok')"],
                stream_output=False,
            )

            summary = summarize_effectiveness(store)

            self.assertEqual(summary["total_replays"], 1)
            self.assertEqual(summary["improved_replays"], 1)
            self.assertGreater(summary["average_score"], 0)
            self.assertTrue(any(line.startswith("instruction_patch:") for line in summary["type_lines"]))
            self.assertTrue(any("win_rate=" in line for line in summary["type_lines"]))
            self.assertTrue(any(line.startswith("top_resolved:") for line in summary["type_lines"]))

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
        '{"repo":"octo/repo","pr_number":19,"pr_changed_files":["src/a.py"],'
        '"review_summary":{"issue_comment_count":0,"review_count":1,"thread_count":1,"unresolved_thread_count":1},'
        '"review_threads":[{"id":"thread-1","is_resolved":false,"path":"src/b.py","line":3,"latest_comment_at":"2026-04-08T11:00:00Z","comments":[]}],'
        '"checks":[{"name":"pytest","bucket":"fail","state":"FAILURE"}],'
        '"ci_runs":[{"failed_log_excerpt":["src/a.py:3: AssertionError","FAILED tests/test_a.py::test_it"]}]}'
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
