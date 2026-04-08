import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from afteraction.github import capture_github_context


class GitHubCaptureTests(unittest.TestCase):
    def test_capture_github_context_collects_threads_checks_runs_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "artifacts" / "run1"
            artifact_dir.mkdir(parents=True, exist_ok=True)

            with (
                patch("afteraction.github.repo_identity", return_value={"nameWithOwner": "octo/repo"}),
                patch(
                    "afteraction.github.pr_snapshot",
                    return_value={
                        "number": 17,
                        "title": "Fix failing PR loop",
                        "url": "https://github.com/octo/repo/pull/17",
                        "reviewDecision": "CHANGES_REQUESTED",
                        "createdAt": "2026-04-08T12:00:00Z",
                        "updatedAt": "2026-04-08T12:30:00Z",
                        "headRefOid": "abc123",
                        "headRefName": "feature",
                        "baseRefName": "master",
                        "files": [{"path": "src/fix.py"}],
                        "commits": [
                            {
                                "commit": {
                                    "oid": "abc123",
                                    "messageHeadline": "attempt fix",
                                    "committedDate": "2026-04-08T12:10:00Z",
                                    "authors": [{"name": "Colin", "user": {"login": "colin"}}],
                                }
                            }
                        ],
                        "comments": [{"id": "issue-comment-1"}],
                        "reviews": [{"id": "review-1"}],
                    },
                ),
                patch(
                    "afteraction.github.fetch_review_threads",
                    return_value=[
                        {
                            "id": "thread-1",
                            "is_resolved": False,
                            "is_outdated": False,
                            "path": "src/problem.py",
                            "line": 42,
                            "latest_comment_at": "2026-04-08T12:15:00Z",
                            "comments": [{"id": "comment-1", "body": "fix this", "created_at": "2026-04-08T12:15:00Z"}],
                        }
                    ],
                ),
                patch(
                    "afteraction.github.pr_checks",
                    return_value=[
                        {
                            "bucket": "fail",
                            "name": "pytest",
                            "state": "FAILURE",
                            "workflow": "CI",
                            "startedAt": "2026-04-08T12:16:00Z",
                            "completedAt": "2026-04-08T12:18:00Z",
                            "link": "https://github.com/octo/repo/actions/runs/99",
                            "description": "tests failed",
                        }
                    ],
                ),
                patch(
                    "afteraction.github.fetch_workflow_runs",
                    return_value=[
                        {
                            "database_id": 99,
                            "workflow_name": "CI",
                            "display_title": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "pull_request",
                            "started_at": "2026-04-08T12:16:00Z",
                            "updated_at": "2026-04-08T12:18:00Z",
                            "url": "https://github.com/octo/repo/actions/runs/99",
                            "jobs": [],
                            "failed_log_artifact": "artifacts/run1/ci_logs/run_99.log",
                            "failed_log_excerpt": ["FAILED tests/test_fix.py::test_loop"],
                        }
                    ],
                ),
            ):
                snapshot = capture_github_context(Path(tmpdir), artifact_dir)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["review_summary"]["unresolved_thread_count"], 1)
            self.assertEqual(snapshot["checks"][0]["name"], "pytest")
            self.assertEqual(snapshot["commit_history"][0]["oid"], "abc123")
            self.assertEqual(snapshot["ci_runs"][0]["database_id"], 99)
            saved = json.loads((artifact_dir / "github_context.json").read_text())
            self.assertEqual(saved["repo"], "octo/repo")


if __name__ == "__main__":
    unittest.main()
