import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from afteragent.config import resolve_paths
from afteragent.diagnostics import analyze_run
from afteragent.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


class EndToEndTests(unittest.TestCase):
    def test_attempt_repair_cli_runner_matrix_records_adapter_events(self) -> None:
        runners = [
            ("shell", "hermes", "AGENTS.md"),
            ("openclaw", "openclaw", "AGENTS.md"),
            ("claude-code", "claude", "CLAUDE.md"),
            ("codex", "codex", "AGENTS.md"),
        ]
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            for _, command_name, _ in runners:
                write_runner_wrapper(bin_dir, command_name)

            for runner_name, command_name, instruction_file in runners:
                with self.subTest(runner=runner_name):
                    repo_dir = root / runner_name
                    init_fixture_repo(
                        repo_dir,
                        instruction_files=[instruction_file] if instruction_file == "CLAUDE.md" else [],
                    )
                    seed_source_run(
                        repo_dir,
                        run_id="seed123",
                        changed_files=["docs/notes.md"],
                        gh_context=python_lint_context(),
                    )

                    run_afteragent(
                        repo_dir,
                        "attempt-repair",
                        "--run-id",
                        "seed123",
                        "--runner",
                        runner_name,
                        "--no-stream",
                        "--summary",
                        f"{runner_name} e2e",
                        "--",
                        command_name,
                        bin_dir=bin_dir,
                    )

                    store = Store(resolve_paths(repo_dir))
                    replay_rows = store.list_replay_runs_for_source("seed123")
                    self.assertEqual(len(replay_rows), 1)
                    replay_run_id = replay_rows[0]["replay_run_id"]
                    comparison = json.loads(replay_rows[0]["comparison_json"])
                    self.assertTrue(replay_rows[0]["intervention_set_id"])
                    self.assertEqual(comparison["verdict"], "improved")
                    self.assertIn("score", comparison)

                    events = store.get_events(replay_run_id)
                    event_types = [event.event_type for event in events]
                    self.assertIn("tool.called", event_types)
                    self.assertIn("file.edited", event_types)
                    self.assertIn("retry.detected", event_types)

                    started = next(event for event in events if event.event_type == "run.started")
                    started_payload = json.loads(started.payload_json)
                    self.assertEqual(started_payload["runner_adapter"], runner_name)

                    manifest_row = store.get_intervention_set(replay_rows[0]["intervention_set_id"])
                    manifest = json.loads(manifest_row["manifest_json"])
                    self.assertEqual(manifest["runner_adapter"], runner_name)
                    self.assertIn(instruction_file, manifest["instruction_targets"])

    def test_golden_fixture_scenarios_preserve_findings_and_apply_targets(self) -> None:
        scenarios = [
            {
                "name": "python_lint",
                "instruction_files": [],
                "changed_files": ["docs/notes.md"],
                "gh_context": python_lint_context(),
                "expected_codes": {
                    "active_ci_failures_present",
                    "low_diff_overlap_with_failing_files",
                },
                "expected_instruction_file": "AGENTS.md",
            },
            {
                "name": "review_mismatch",
                "instruction_files": ["CLAUDE.md"],
                "changed_files": ["docs/notes.md"],
                "gh_context": review_mismatch_context(),
                "expected_codes": {
                    "unresolved_review_threads_present",
                    "comments_ignored_after_they_existed",
                    "low_diff_overlap_with_failing_files",
                },
                "expected_instruction_file": "CLAUDE.md",
            },
        ]
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for scenario in scenarios:
                with self.subTest(scenario=scenario["name"]):
                    repo_dir = root / scenario["name"]
                    init_fixture_repo(repo_dir, instruction_files=scenario["instruction_files"])
                    seed_source_run(
                        repo_dir,
                        run_id="golden123",
                        changed_files=scenario["changed_files"],
                        gh_context=scenario["gh_context"],
                    )

                    run_afteragent(repo_dir, "apply-interventions", "golden123")

                    store = Store(resolve_paths(repo_dir))
                    findings, interventions = analyze_run(store, "golden123")
                    codes = {item.code for item in findings}
                    self.assertTrue(scenario["expected_codes"].issubset(codes))
                    self.assertTrue(any(item.target == "repo_instructions" for item in interventions))

                    rows = store.list_intervention_sets_for_run("golden123")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["kind"], "applied")
                    manifest = json.loads(rows[0]["manifest_json"])
                    self.assertIn(scenario["expected_instruction_file"], manifest["instruction_targets"])

                    target_path = repo_dir / scenario["expected_instruction_file"]
                    self.assertTrue(target_path.exists())
                    target_text = target_path.read_text()
                    self.assertIn("AfterAction Interventions", target_text)


def run_afteragent(repo_dir: Path, *args: str, bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [sys.executable, "-m", "afteragent.cli", *args],
        cwd=str(repo_dir),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def init_fixture_repo(repo_dir: Path, instruction_files: list[str]) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "src").mkdir(exist_ok=True)
    (repo_dir / "docs").mkdir(exist_ok=True)
    (repo_dir / "src" / "app.py").write_text("print('hello')\n")
    (repo_dir / "docs" / "notes.md").write_text("# notes\n")
    for name in instruction_files:
        (repo_dir / name).write_text(f"# {name}\n")
    run_git(repo_dir, "init")
    run_git(repo_dir, "config", "user.email", "afteragent@example.com")
    run_git(repo_dir, "config", "user.name", "AfterAction Tests")
    run_git(repo_dir, "add", ".")
    run_git(repo_dir, "commit", "-m", "init")


def run_git(repo_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
        check=True,
    )


def seed_source_run(
    repo_dir: Path,
    run_id: str,
    changed_files: list[str],
    gh_context: dict,
) -> None:
    store = Store(resolve_paths(repo_dir))
    store.create_run(run_id, "python3 agent.py", str(repo_dir), "2026-04-08T12:00:00Z", summary="seed")
    store.finish_run(run_id, "failed", 1, "2026-04-08T12:02:00Z", 1000, summary="seed")
    artifact_dir = store.run_artifact_dir(run_id)
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("AssertionError: failed\n")
    (artifact_dir / "git_diff_after.patch").write_text(patch_for_files(changed_files))
    (artifact_dir / "github_context.json").write_text(json.dumps(gh_context))


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


def write_runner_wrapper(bin_dir: Path, name: str) -> None:
    script = bin_dir / name
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "print(os.environ.get('AFTERACTION_RUNNER_ADAPTER', ''))",
                "print('tool: apply_patch')",
                "print('edited src/app.py')",
                "print('retrying 2')",
            ]
        )
        + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def python_lint_context() -> dict:
    return {
        "repo": "octo/repo",
        "pr_number": 11,
        "pr_changed_files": ["docs/notes.md"],
        "review_summary": {
            "issue_comment_count": 0,
            "review_count": 0,
            "thread_count": 0,
            "unresolved_thread_count": 0,
        },
        "review_threads": [],
        "checks": [{"name": "pytest", "bucket": "fail", "state": "FAILURE"}],
        "ci_runs": [
            {
                "failed_log_excerpt": [
                    "pyproject.toml:1: Failed to parse file",
                    "tests/test_app.py:9: AssertionError",
                ]
            }
        ],
    }


def review_mismatch_context() -> dict:
    return {
        "repo": "octo/repo",
        "pr_number": 12,
        "pr_changed_files": ["docs/notes.md"],
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
                "path": "src/review.py",
                "line": 5,
                "latest_comment_at": "2026-04-08T11:30:00Z",
                "comments": [],
            }
        ],
        "checks": [],
        "ci_runs": [],
    }


if __name__ == "__main__":
    unittest.main()
