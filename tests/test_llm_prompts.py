import tempfile
from pathlib import Path

from afteragent.config import resolve_paths
from afteragent.llm.prompts import DiagnosisContext, load_diagnosis_context
from afteragent.models import PatternFinding
from afteragent.store import Store


def _seed_run_with_artifacts(tmp: Path, run_id: str = "run1") -> Store:
    store = Store(resolve_paths(tmp))
    store.create_run(run_id, "python3 -c 'print(1)'", str(tmp), "2026-04-10T12:00:00Z")

    artifact_dir = store.run_artifact_dir(run_id)
    (artifact_dir / "stdout.log").write_text("first line\nsecond line\nthird line\n")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text(
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )

    store.finish_run(run_id, "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")
    return store


def test_load_diagnosis_context_returns_run_metadata(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    assert ctx.run.id == "run1"
    assert ctx.run.command == "python3 -c 'print(1)'"
    assert ctx.run.status == "passed"


def test_load_diagnosis_context_includes_stdout_head_and_tail(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    stdout_lines = [f"line {i}" for i in range(200)]
    (artifact_dir / "stdout.log").write_text("\n".join(stdout_lines) + "\n")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    ctx = load_diagnosis_context(store, "run1")

    assert "line 0" in ctx.stdout_head
    assert "line 49" in ctx.stdout_head
    assert "line 150" in ctx.stdout_tail
    assert "line 199" in ctx.stdout_tail
    assert "line 100" not in ctx.stdout_head
    assert "line 100" not in ctx.stdout_tail


def test_load_diagnosis_context_caps_head_and_tail_char_budget(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    stdout_lines = ["x" * 500 for _ in range(50)]
    (artifact_dir / "stdout.log").write_text("\n".join(stdout_lines) + "\n")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    ctx = load_diagnosis_context(store, "run1")
    assert len(ctx.stdout_head) <= 5000
    assert len(ctx.stdout_tail) <= 5000


def test_load_diagnosis_context_includes_diff_and_changed_files(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    assert "diff --git a/foo.py" in ctx.diff_text
    assert "foo.py" in ctx.changed_files


def test_load_diagnosis_context_truncates_massive_diff(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text(
        "diff --git a/x.py b/x.py\n" + ("x" * 50_000) + "\n"
    )
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    ctx = load_diagnosis_context(store, "run1")
    assert len(ctx.diff_text) <= 21_000  # 20k + truncation marker
    assert "[diff truncated" in ctx.diff_text


def test_load_diagnosis_context_includes_rule_findings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    store.replace_diagnosis(
        "run1",
        [{
            "run_id": "run1",
            "code": "seed_finding",
            "title": "Seeded for test",
            "severity": "medium",
            "summary": "x",
            "evidence_json": "[]",
        }],
        [],
    )

    ctx = load_diagnosis_context(store, "run1")
    rule_codes = [f.code for f in ctx.rule_findings]
    assert "seed_finding" in rule_codes


def test_load_diagnosis_context_includes_transcript_events(tmp_path):
    from afteragent.transcripts import (
        KIND_FILE_READ,
        SOURCE_CLAUDE_CODE_JSONL,
        TranscriptEvent,
    )

    store = _seed_run_with_artifacts(tmp_path)
    store.add_transcript_events(
        "run1",
        [
            TranscriptEvent(
                run_id="run1",
                sequence=0,
                kind=KIND_FILE_READ,
                tool_name="Read",
                target="/repo/foo.py",
                source=SOURCE_CLAUDE_CODE_JSONL,
                raw_ref="line:10",
                timestamp="2026-04-10T12:00:01Z",
            ),
        ],
    )

    ctx = load_diagnosis_context(store, "run1")
    assert len(ctx.transcript_events) == 1
    assert ctx.transcript_events[0].kind == "file_read"


def test_load_diagnosis_context_github_summary_missing_is_none(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    assert ctx.github_summary is None


def test_load_diagnosis_context_rejects_unknown_run(tmp_path):
    import pytest as _pytest

    store = Store(resolve_paths(tmp_path))
    with _pytest.raises(ValueError, match="Run not found"):
        load_diagnosis_context(store, "does-not-exist")
