import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from afteragent.config import resolve_paths
from afteragent.effectiveness import (
    EffectivenessMetric,
    EffectivenessReport,
    compute_effectiveness_metrics,
)
from afteragent.store import Store


def _make_store(tmp: Path) -> Store:
    return Store(resolve_paths(tmp))


def _seed_run(store: Store, run_id: str = "run1") -> None:
    store.create_run(run_id, "echo hi", "/tmp", "2026-04-10T12:00:00Z")


def _seed_diagnosis(
    store: Store,
    run_id: str,
    findings: list[tuple[str, str]],  # (code, source)
) -> None:
    """Seed rule-based and/or llm diagnoses for a run."""
    rule_rows = [
        {
            "run_id": run_id,
            "code": code,
            "title": f"Title for {code}",
            "severity": "medium",
            "summary": f"Summary for {code}",
            "evidence_json": "[]",
        }
        for code, source in findings
        if source == "rule"
    ]
    llm_rows = [
        {
            "run_id": run_id,
            "code": code,
            "title": f"Title for {code}",
            "severity": "medium",
            "summary": f"Summary for {code}",
            "evidence_json": "[]",
            "source": "llm",
        }
        for code, source in findings
        if source == "llm"
    ]
    if rule_rows:
        store.replace_diagnosis(run_id, rule_rows, [])
    if llm_rows:
        store.replace_llm_diagnosis(
            run_id,
            findings_rows=llm_rows,
            interventions_rows=[],
        )


def _seed_replay(
    store: Store,
    source_run_id: str,
    replay_run_id: str,
    intervention_set_id: str,
    comparison: dict,
    manifest: dict,
    created_at: str = "2026-04-10T13:00:00Z",
) -> None:
    """Seed a complete replay row + the intervention_sets row it joins against."""
    # Create the replay_run record first.
    store.create_run(replay_run_id, "echo replay", "/tmp", created_at)
    store.finish_run(
        replay_run_id, "passed", 0, created_at, 1000, summary="ok",
    )
    # Create the intervention set (the join target for list_all_replay_runs).
    store.save_intervention_set(
        set_id=intervention_set_id,
        source_run_id=source_run_id,
        version=1,
        kind="export",
        created_at=created_at,
        output_dir="/tmp/exports/fake",
        manifest=manifest,
    )
    # Record the replay row with the comparison payload.
    store.record_replay_run(
        source_run_id=source_run_id,
        replay_run_id=replay_run_id,
        intervention_set_id=intervention_set_id,
        created_at=created_at,
        applied_before_replay=True,
        comparison=comparison,
    )


def test_empty_store_returns_zero_total_replays():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        report = compute_effectiveness_metrics(store)
        assert isinstance(report, EffectivenessReport)
        assert report.total_replays == 0
        assert report.finding_metrics == []
        assert report.intervention_metrics == []
        assert report.min_samples_threshold == 5


def test_returns_finding_code_resolution_rate():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed 6 source runs, each with code "low_diff_overlap".
        # 4 of them have the code resolved in the replay.
        for i in range(6):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("low_diff_overlap", "rule")])
            resolved = ["low_diff_overlap"] if i < 4 else []
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={
                    "resolved_findings": resolved,
                    "new_findings": [],
                    "improved": i < 4,
                    "score": 10 if i < 4 else -5,
                },
                manifest={"interventions": []},
            )

        report = compute_effectiveness_metrics(store)
        assert report.total_replays == 6
        assert len(report.finding_metrics) == 1
        metric = report.finding_metrics[0]
        assert metric.key == "low_diff_overlap"
        assert metric.kind == "finding_code"
        assert metric.samples == 6
        assert metric.successes == 4
        assert abs(metric.success_rate - (4 / 6)) < 1e-9
        assert metric.source == "rule"


def test_respects_min_samples_threshold():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed 3 replays for code X (below default threshold 5).
        # Seed 6 replays for code Y (above threshold).
        for i in range(3):
            source_id = f"x_src{i}"
            replay_id = f"x_rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("rare_code", "rule")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"x_iset{i}",
                comparison={"resolved_findings": [], "improved": False, "score": 0},
                manifest={"interventions": []},
            )
        for i in range(6):
            source_id = f"y_src{i}"
            replay_id = f"y_rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("common_code", "rule")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"y_iset{i}",
                comparison={"resolved_findings": ["common_code"], "improved": True, "score": 10},
                manifest={"interventions": []},
            )

        report = compute_effectiveness_metrics(store)
        assert report.total_replays == 9
        codes = [m.key for m in report.finding_metrics]
        assert "rare_code" not in codes
        assert "common_code" in codes


def test_min_samples_parameter_is_honored():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed 3 replays; with min_samples=3, the metric should appear.
        for i in range(3):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("code_x", "rule")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": ["code_x"], "improved": True, "score": 10},
                manifest={"interventions": []},
            )

        default_report = compute_effectiveness_metrics(store)
        low_threshold_report = compute_effectiveness_metrics(store, min_samples=3)

        assert len(default_report.finding_metrics) == 0
        assert len(low_threshold_report.finding_metrics) == 1
        assert low_threshold_report.min_samples_threshold == 3


def test_intervention_pair_win_rate():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed 5 replays with prompt_patch/task_prompt interventions.
        # 3 of them improved.
        for i in range(5):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            improved = i < 3
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": [], "improved": improved, "score": 5 if improved else -5},
                manifest={
                    "interventions": [
                        {
                            "type": "prompt_patch",
                            "target": "task_prompt",
                            "title": "x",
                            "content": "x",
                            "scope": "pr",
                            "source": "rule",
                        }
                    ]
                },
            )

        report = compute_effectiveness_metrics(store)
        assert len(report.intervention_metrics) == 1
        metric = report.intervention_metrics[0]
        assert metric.key == "prompt_patch/task_prompt"
        assert metric.kind == "intervention_type_target"
        assert metric.samples == 5
        assert metric.successes == 3
        assert abs(metric.success_rate - 0.6) < 1e-9
        assert metric.source == "rule"


def test_intervention_pair_deduplicated_per_replay():
    """A manifest with five interventions of the same (type, target) pair
    must count as one sample, not five."""
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed 5 replays. Each manifest has THREE prompt_patch/task_prompt
        # interventions — they should count as 1 sample per replay.
        for i in range(5):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": [], "improved": True, "score": 10},
                manifest={
                    "interventions": [
                        {"type": "prompt_patch", "target": "task_prompt", "title": "a", "content": "a", "scope": "pr", "source": "rule"},
                        {"type": "prompt_patch", "target": "task_prompt", "title": "b", "content": "b", "scope": "pr", "source": "rule"},
                        {"type": "prompt_patch", "target": "task_prompt", "title": "c", "content": "c", "scope": "pr", "source": "rule"},
                    ]
                },
            )

        report = compute_effectiveness_metrics(store)
        metric = report.intervention_metrics[0]
        assert metric.samples == 5  # not 15
        assert metric.successes == 5


def test_source_tagging_rule_only():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        for i in range(5):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("rule_only_code", "rule")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": [], "improved": False, "score": 0},
                manifest={"interventions": []},
            )

        report = compute_effectiveness_metrics(store)
        assert report.finding_metrics[0].source == "rule"


def test_source_tagging_llm_only():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        for i in range(5):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("llm_only_code", "llm")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": [], "improved": False, "score": 0},
                manifest={"interventions": []},
            )

        report = compute_effectiveness_metrics(store)
        assert report.finding_metrics[0].source == "llm"


def test_source_tagging_mixed():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # 3 rule, 2 llm source runs for the same code.
        for i in range(3):
            source_id = f"r{i}"
            replay_id = f"rrep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("mixed_code", "rule")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"riset{i}",
                comparison={"resolved_findings": [], "improved": False, "score": 0},
                manifest={"interventions": []},
            )
        for i in range(2):
            source_id = f"l{i}"
            replay_id = f"lrep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("mixed_code", "llm")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"liset{i}",
                comparison={"resolved_findings": [], "improved": False, "score": 0},
                manifest={"interventions": []},
            )

        report = compute_effectiveness_metrics(store)
        assert report.finding_metrics[0].source == "mixed"


def test_sort_order_by_samples_then_rate_then_key():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Three codes:
        #  - "alpha": 10 samples, 50% rate
        #  - "beta":  10 samples, 80% rate
        #  - "gamma":  7 samples, 100% rate
        # Expected order: beta, alpha, gamma (samples desc, then rate desc).
        def _seed_group(code: str, total: int, successes: int, prefix: str) -> None:
            for i in range(total):
                source_id = f"{prefix}_src{i}"
                replay_id = f"{prefix}_rep{i}"
                _seed_run(store, source_id)
                _seed_diagnosis(store, source_id, [(code, "rule")])
                resolved = [code] if i < successes else []
                _seed_replay(
                    store,
                    source_run_id=source_id,
                    replay_run_id=replay_id,
                    intervention_set_id=f"{prefix}_iset{i}",
                    comparison={"resolved_findings": resolved, "improved": i < successes, "score": 5},
                    manifest={"interventions": []},
                )

        _seed_group("alpha", 10, 5, "a")
        _seed_group("beta", 10, 8, "b")
        _seed_group("gamma", 7, 7, "g")

        report = compute_effectiveness_metrics(store)
        keys = [m.key for m in report.finding_metrics]
        assert keys == ["beta", "alpha", "gamma"]


def test_skips_corrupt_replay_rows():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed 6 valid replays (1 more than the default threshold of 5
        # so that after corrupting one row, we still have 5 clean samples).
        for i in range(6):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_diagnosis(store, source_id, [("code_x", "rule")])
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": ["code_x"], "improved": True, "score": 10},
                manifest={"interventions": []},
            )

        # Corrupt one row by direct DB write.
        with store.connection() as conn:
            conn.execute(
                "UPDATE replay_runs SET comparison_json = 'not json' WHERE replay_run_id = 'rep0'"
            )

        report = compute_effectiveness_metrics(store)
        # total_replays counts all table rows, including the corrupt one.
        assert report.total_replays == 6
        # The metric is computed from the 5 clean rows only.
        metrics_for_x = [m for m in report.finding_metrics if m.key == "code_x"]
        assert len(metrics_for_x) == 1
        assert metrics_for_x[0].samples == 5
        assert metrics_for_x[0].successes == 5


def test_handles_deleted_source_run():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Seed a replay whose source run has no diagnoses at all.
        _seed_run(store, "orphan_source")
        # Note: no _seed_diagnosis call — simulates a run whose findings
        # were never recorded or were cleared.
        _seed_replay(
            store,
            source_run_id="orphan_source",
            replay_run_id="rep_orphan",
            intervention_set_id="iset_orphan",
            comparison={"resolved_findings": [], "improved": True, "score": 5},
            manifest={
                "interventions": [
                    {
                        "type": "prompt_patch",
                        "target": "task_prompt",
                        "title": "x",
                        "content": "x",
                        "scope": "pr",
                        "source": "rule",
                    }
                ]
            },
        )
        # Add 4 more runs with the same intervention pair so the threshold fires.
        for i in range(4):
            source_id = f"src{i}"
            replay_id = f"rep{i}"
            _seed_run(store, source_id)
            _seed_replay(
                store,
                source_run_id=source_id,
                replay_run_id=replay_id,
                intervention_set_id=f"iset{i}",
                comparison={"resolved_findings": [], "improved": True, "score": 5},
                manifest={
                    "interventions": [
                        {
                            "type": "prompt_patch",
                            "target": "task_prompt",
                            "title": "x",
                            "content": "x",
                            "scope": "pr",
                            "source": "rule",
                        }
                    ]
                },
            )

        report = compute_effectiveness_metrics(store)
        # The orphan replay still contributes to intervention metrics.
        intervention_metric = report.intervention_metrics[0]
        assert intervention_metric.samples == 5
        # Finding metrics are empty because no source run had diagnoses.
        assert report.finding_metrics == []
