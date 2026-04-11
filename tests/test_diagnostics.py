import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteragent.config import AppPaths
from afteragent.diagnostics import build_interventions, count_changed_files, extract_failure_files
from afteragent.models import PatternFinding
from afteragent.store import Store


class DiagnosticsTests(unittest.TestCase):
    def test_count_changed_files_counts_unique_paths(self) -> None:
        diff = """diff --git a/a.py b/a.py
diff --git a/b.py b/b.py
diff --git a/a.py b/a.py
"""
        self.assertEqual(count_changed_files(diff), 2)

    def test_build_interventions_maps_findings(self) -> None:
        findings = [
            PatternFinding(
                code="active_ci_failures_present",
                title="Active CI failures present",
                severity="high",
                summary="x",
                evidence=[],
            ),
            PatternFinding(
                code="unresolved_review_threads_present",
                title="Unresolved review threads present",
                severity="medium",
                summary="x",
                evidence=[],
            ),
            PatternFinding(
                code="same_failure_repeated_across_runs",
                title="Same failure repeated across runs",
                severity="medium",
                summary="x",
                evidence=[],
            ),
        ]
        interventions = build_interventions(findings)
        self.assertEqual(
            [item.type for item in interventions],
            [
                "context_injection_rule",
                "instruction_patch",
                "prompt_patch",
                "runtime_guardrail",
            ],
        )
        self.assertEqual(
            [item.target for item in interventions],
            [
                "runner_context",
                "repo_instructions",
                "task_prompt",
                "runner_policy",
            ],
        )
        self.assertTrue(all(item.scope == "pr" for item in interventions))

    def test_analyze_run_uses_cross_run_pr_signals(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            paths = AppPaths(
                root=root,
                db_path=root / "afteragent.sqlite3",
                artifacts_dir=root / "artifacts",
                exports_dir=root / "exports",
                applied_dir=root / "applied",
                replays_dir=root / "replays",
                config_path=root / "config.toml",
            )
            store = Store(paths)

            store.create_run(
                "prev123",
                "python3 agent.py",
                "/repo",
                "2026-04-08T12:00:00Z",
                summary="previous run",
            )
            store.finish_run(
                "prev123",
                "failed",
                1,
                "2026-04-08T12:02:00Z",
                1000,
                summary="failed before",
            )
            prev_dir = store.run_artifact_dir("prev123")
            (prev_dir / "stdout.log").write_text("")
            (prev_dir / "stderr.log").write_text("AssertionError: widget loop failed\n")
            (prev_dir / "git_diff_after.patch").write_text(
                patch_for_files(["src/alpha.py", "src/beta.py"])
            )
            (prev_dir / "github_context.json").write_text(
                json.dumps(
                    {
                        "repo": "octo/repo",
                        "pr_number": 17,
                        "review_threads": [],
                        "checks": [{"name": "pytest", "bucket": "fail", "state": "FAILURE"}],
                        "ci_runs": [
                            {
                                "failed_log_excerpt": [
                                    "FAILED tests/test_widget.py::test_loop",
                                    "AssertionError: widget loop failed",
                                ]
                            }
                        ],
                    }
                )
            )

            store.create_run(
                "curr123",
                "python3 agent.py",
                "/repo",
                "2026-04-08T13:00:00Z",
                summary="current run",
            )
            store.finish_run(
                "curr123",
                "failed",
                1,
                "2026-04-08T13:03:00Z",
                1200,
                summary="failed again",
            )
            current_dir = store.run_artifact_dir("curr123")
            (current_dir / "stdout.log").write_text("")
            (current_dir / "stderr.log").write_text("AssertionError: widget loop failed\n")
            (current_dir / "git_diff_after.patch").write_text(
                patch_for_files(
                    [
                        "src/gamma.py",
                        "src/delta.py",
                        "src/epsilon.py",
                        "src/zeta.py",
                        "src/eta.py",
                        "src/theta.py",
                        "src/iota.py",
                        "src/kappa.py",
                    ]
                )
            )
            (current_dir / "github_context.json").write_text(
                json.dumps(
                    {
                        "repo": "octo/repo",
                        "pr_number": 17,
                        "review_threads": [
                            {
                                "id": "thread-1",
                                "is_resolved": False,
                                "path": "src/problem.py",
                                "latest_comment_at": "2026-04-08T12:30:00Z",
                                "comments": [],
                            }
                        ],
                        "checks": [{"name": "pytest", "bucket": "fail", "state": "FAILURE"}],
                        "ci_runs": [
                            {
                                "failed_log_excerpt": [
                                    "FAILED tests/test_widget.py::test_loop",
                                    "AssertionError: widget loop failed",
                                ]
                            }
                        ],
                    }
                )
            )

            from afteragent.diagnostics import analyze_run

            findings, interventions = analyze_run(store, "curr123")

            codes = {finding.code for finding in findings}
            self.assertIn("same_failure_repeated_across_runs", codes)
            self.assertIn("low_diff_overlap_with_failing_files", codes)
            self.assertIn("comments_ignored_after_they_existed", codes)
            self.assertIn("broad_edit_drift", codes)
            self.assertEqual(
                {item.type for item in interventions},
                {"instruction_patch", "prompt_patch", "runtime_guardrail", "tool_policy_rule"},
            )

    def test_analyze_github_validation_run_emits_github_only_findings(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".afteragent"
            paths = AppPaths(
                root=root,
                db_path=root / "afteragent.sqlite3",
                artifacts_dir=root / "artifacts",
                exports_dir=root / "exports",
                applied_dir=root / "applied",
                replays_dir=root / "replays",
                config_path=root / "config.toml",
            )
            store = Store(paths)

            store.create_run(
                "ghonly1",
                "github-pr-validation octo/repo#19",
                "/repo",
                "2026-04-08T14:00:00Z",
                summary="github validation",
            )
            store.finish_run(
                "ghonly1",
                "failed",
                1,
                "2026-04-08T14:00:10Z",
                0,
                summary="validation failure",
            )
            artifact_dir = store.run_artifact_dir("ghonly1")
            (artifact_dir / "stdout.log").write_text("")
            (artifact_dir / "stderr.log").write_text("")
            (artifact_dir / "git_diff_after.patch").write_text("")
            (artifact_dir / "github_context.json").write_text(
                json.dumps(
                    {
                        "repo": "octo/repo",
                        "pr_number": 19,
                        "pr_changed_files": [
                            ".github/workflows/build.yml",
                            "README.md",
                            "RELEASING.md",
                        ],
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
                                "path": "pyproject.toml",
                                "line": 8,
                                "latest_comment_at": "2026-04-08T13:59:00Z",
                                "comments": [],
                            }
                        ],
                        "checks": [
                            {"name": "ruff-mypy", "bucket": "fail", "state": "FAILURE"},
                            {"name": "py 3.10", "bucket": "fail", "state": "FAILURE"},
                        ],
                        "ci_runs": [
                            {
                                "failed_log_excerpt": [
                                    "error[RUF200]: Failed to parse pyproject.toml: invalid type: map, expected a sequence",
                                    "tests/test_altairplot.py:99: AssertionError",
                                ]
                            }
                        ],
                    }
                )
            )

            from afteragent.diagnostics import analyze_run

            findings, interventions = analyze_run(store, "ghonly1")
            codes = {finding.code for finding in findings}
            self.assertIn("active_ci_failures_present", codes)
            self.assertIn("unresolved_review_threads_present", codes)
            self.assertIn("comments_ignored_after_they_existed", codes)
            self.assertIn("low_diff_overlap_with_failing_files", codes)
            self.assertEqual(
                {item.type for item in interventions},
                {"instruction_patch", "prompt_patch", "context_injection_rule"},
            )

    def test_extract_failure_files_filters_noisy_ci_tokens(self) -> None:
        gh_context = {
            "pr_changed_files": [
                ".github/workflows/build.yml",
                ".github/workflows/lint.yml",
                "README.md",
                "RELEASING.md",
            ],
            "review_threads": [],
            "ci_runs": [
                {
                    "failed_log_excerpt": [
                        "error[RUF200]: Failed to parse pyproject.toml: invalid type: map, expected a sequence",
                        'assert result.count("https://cdn.jsdelivr.net/npm/vega@") == 1',
                        "E       assert '<!DOCTYPE html> ... v...pyright ... </html>' in result",
                        "tests/test_altairplot.py:99: AssertionError",
                        r'errors_warnings\.rst:5\n.+polars\.DataFrame\(\{"a": \[1, 2, 3\]\}\)',
                    ]
                }
            ],
        }

        files = extract_failure_files("", "", gh_context)

        self.assertEqual(
            files,
            {"pyproject.toml", "tests/test_altairplot.py", "errors_warnings.rst"},
        )


def patch_for_files(paths: list[str]) -> str:
    chunks = []
    for path in paths:
        chunks.append(
            "\n".join(
                [
                    f"diff --git a/{path} b/{path}",
                    f"--- a/{path}",
                    f"+++ b/{path}",
                    "@@ -1 +1 @@",
                    "+change",
                ]
            )
        )
    return "\n".join(chunks)


if __name__ == "__main__":
    unittest.main()
