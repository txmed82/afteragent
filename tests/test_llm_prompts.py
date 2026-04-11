import tempfile
from pathlib import Path

from afteragent.config import resolve_paths
from afteragent.effectiveness import EffectivenessMetric, EffectivenessReport
from afteragent.llm.prompts import (
    DiagnosisContext,
    MergedFinding,
    build_diagnosis_prompt,
    build_interventions_prompt,
    estimate_tokens,
    load_diagnosis_context,
)
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


def test_estimate_tokens_is_proportional_to_character_count():
    assert 80 <= estimate_tokens("x" * 400) <= 120


def test_build_diagnosis_prompt_returns_system_and_user_strings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    system, user = build_diagnosis_prompt(ctx)

    assert isinstance(system, str) and isinstance(user, str)
    assert "diagnostician" in system.lower()
    assert ctx.run.id in user or ctx.run.command in user


def test_build_diagnosis_prompt_includes_rule_findings_section_when_present(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    store.replace_diagnosis(
        "run1",
        [{
            "run_id": "run1",
            "code": "seed_finding",
            "title": "Seeded for test",
            "severity": "medium",
            "summary": "a rule was confused",
            "evidence_json": '["hint1", "hint2"]',
        }],
        [],
    )
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "seed_finding" in user
    assert "Seeded for test" in user


def test_build_diagnosis_prompt_omits_rule_findings_section_when_empty(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    # When empty, the section either shows "(none)" or is absent.
    assert "## Rule-based findings" not in user or "(none)" in user


def test_build_diagnosis_prompt_includes_transcript_events_when_present(tmp_path):
    from afteragent.transcripts import (
        KIND_FILE_READ,
        SOURCE_CLAUDE_CODE_JSONL,
        TranscriptEvent,
    )

    store = _seed_run_with_artifacts(tmp_path)
    store.add_transcript_events("run1", [
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
    ])
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "/repo/foo.py" in user
    assert "file_read" in user


def test_build_diagnosis_prompt_respects_token_budget(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    from afteragent.transcripts import KIND_BASH_COMMAND, SOURCE_CLAUDE_CODE_JSONL, TranscriptEvent
    events = [
        TranscriptEvent(
            run_id="run1",
            sequence=i,
            kind=KIND_BASH_COMMAND,
            tool_name="Bash",
            target=f"some-long-command-with-lots-of-context-{i} " + ("x" * 100),
            source=SOURCE_CLAUDE_CODE_JSONL,
            raw_ref=f"line:{i}",
            inputs_summary="x" * 150,
            output_excerpt="x" * 200,
            timestamp="2026-04-10T12:00:00Z",
        )
        for i in range(500)
    ]
    store.add_transcript_events("run1", events)

    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)

    assert estimate_tokens(user) <= 25_000


def test_build_interventions_prompt_includes_merged_findings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    merged = [
        MergedFinding(
            code="novel_loop",
            title="Agent in edit loop",
            severity="high",
            summary="edited same file 4 times",
            evidence=["foo.py edited at t=0", "foo.py edited at t=10"],
            source="llm",
        )
    ]

    system, user = build_interventions_prompt(ctx, merged)
    assert "author" in system.lower() and "intervention" in system.lower()
    assert "novel_loop" in user
    assert "Agent in edit loop" in user


def test_build_interventions_prompt_handles_empty_merged_findings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    system, user = build_interventions_prompt(ctx, [])
    assert isinstance(system, str) and isinstance(user, str)
    assert len(system) > 0 and len(user) > 0


def _sample_report() -> EffectivenessReport:
    return EffectivenessReport(
        total_replays=10,
        min_samples_threshold=5,
        finding_metrics=[
            EffectivenessMetric(
                key="low_diff_overlap",
                kind="finding_code",
                source="rule",
                samples=10,
                successes=8,
                success_rate=0.8,
            ),
        ],
        intervention_metrics=[
            EffectivenessMetric(
                key="prompt_patch/task_prompt",
                kind="intervention_type_target",
                source="mixed",
                samples=8,
                successes=6,
                success_rate=0.75,
            ),
        ],
        generated_at="2026-04-11T12:00:00Z",
    )


def test_build_diagnosis_prompt_includes_effectiveness_section_when_report_passed(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    report = _sample_report()

    _, user = build_diagnosis_prompt(ctx, effectiveness_report=report)

    assert "## Historical effectiveness (finding codes)" in user
    assert "code=low_diff_overlap" in user
    assert "80%" in user


def test_build_diagnosis_prompt_omits_effectiveness_section_when_report_none(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    _, user = build_diagnosis_prompt(ctx, effectiveness_report=None)

    assert "## Historical effectiveness" not in user


def test_build_interventions_prompt_includes_effectiveness_section_when_report_passed(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    report = _sample_report()

    _, user = build_interventions_prompt(
        ctx, merged_findings=[], effectiveness_report=report
    )

    assert "## Historical effectiveness (intervention type/target)" in user
    assert "pair=prompt_patch/task_prompt" in user
    assert "75%" in user


def test_build_interventions_prompt_omits_effectiveness_section_when_report_none(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    _, user = build_interventions_prompt(
        ctx, merged_findings=[], effectiveness_report=None
    )

    assert "## Historical effectiveness" not in user


def test_build_diagnosis_prompt_with_effectiveness_respects_token_budget(tmp_path):
    """Adding the effectiveness block should not push the prompt over budget
    on a typical-sized run."""
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    report = _sample_report()

    _, user = build_diagnosis_prompt(ctx, effectiveness_report=report)

    assert estimate_tokens(user) <= 25_000


def test_build_diagnosis_prompt_includes_task_prompt_section_when_set(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    # Update the run's task_prompt so the context loader picks it up.
    store.set_run_task_prompt("run1", "implement dark mode toggle")
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "## Task prompt" in user
    assert "implement dark mode toggle" in user


def test_build_diagnosis_prompt_omits_task_prompt_section_when_null(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    # No set_run_task_prompt call — the field is NULL.
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "## Task prompt" not in user
