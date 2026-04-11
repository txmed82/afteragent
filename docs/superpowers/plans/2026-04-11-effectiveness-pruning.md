# Effectiveness-Driven Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an effectiveness-aggregation layer that reads historical replay data from the existing `replay_runs` table, surfaces per-finding and per-intervention-pair win rates to the LLM prompts as soft feedback, and exposes a new `afteragent stats` CLI subcommand for direct inspection. Purely additive over sub-projects 1–2.

**Architecture:** New top-level `src/afteragent/effectiveness.py` module with one aggregator function + two formatters. The `enhancer.py` from sub-project 2 computes an `EffectivenessReport` once per enhance call and threads it into both prompt builders via a new optional parameter. Failure in effectiveness computation falls back cleanly to sub-project 2 behavior. No schema changes, no UI changes, no rule detector changes.

**Tech Stack:** Python 3.11+, stdlib only. Consumes the existing `replay_runs` / `intervention_sets` tables from sub-project 0 and the `diagnoses.source` column from sub-project 2. Uses `json` stdlib for manifest parsing. No new dependencies.

---

## Reference documents

- **Spec:** `docs/superpowers/specs/2026-04-11-effectiveness-pruning-design.md` — source of truth for design decisions and rationale. Read first if anything in this plan is unclear.
- **Sub-project 2 plan:** `docs/superpowers/plans/2026-04-10-llm-diagnosis.md` — sets the codebase conventions this plan follows.

## Pre-flight notes

1. **Branch:** this plan was written against `afteragent-subproject-3` branched from merged master (`b90c7dc`, post-PR #6). Do not push/pull/fetch/switch branches mid-execution. Commit locally only.
2. **Existing test count:** 179 pytest (after sub-project 2 merged) + 28 unittest + 2 e2e. Every task's final verification step must preserve "all existing tests pass."
3. **No new dependencies.** `pyproject.toml` stays untouched. The aggregator uses stdlib `json`, `dataclasses`, `sqlite3.Row` — everything already available.
4. **Commit style:** follow existing commits, imperative mood, one-line subject. Include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>` footer on every commit.
5. **Test runner:** `python3 -m pytest` from the repo root. Individual test: `python3 -m pytest tests/test_name.py::test_func -v`.
6. **Existing store method used by the aggregator:** `store.list_all_replay_runs()` at `src/afteragent/store.py:664`. Returns `list[sqlite3.Row]` where each row has columns `source_run_id`, `replay_run_id`, `intervention_set_id`, `created_at`, `applied_before_replay`, `comparison_json`, `replay_status`, `replay_exit_code`, `replay_summary`, `intervention_manifest_json`.
7. **Existing store method for source findings:** `store.get_diagnoses(run_id)` returns `list[sqlite3.Row]` with columns `code`, `title`, `severity`, `summary`, `evidence_json`, `source`. The `source` column was added in sub-project 2 with values `"rule"` or `"llm"`.
8. **`now_utc()` helper:** from `src/afteragent/models.py`. Returns ISO-8601 timestamps in UTC.

## File structure

**New files:**

```
src/afteragent/effectiveness.py         # EffectivenessMetric, EffectivenessReport, compute_effectiveness_metrics, format_metrics_for_prompt, format_metrics_for_cli
tests/test_effectiveness.py             # ~18 unit tests
```

**Modified files:**

```
src/afteragent/llm/prompts.py           # build_diagnosis_prompt + build_interventions_prompt gain optional effectiveness_report
src/afteragent/llm/enhancer.py          # computes EffectivenessReport once, threads through both prompt calls, try/except fallback
src/afteragent/cli.py                   # new `stats` subcommand + `--min-samples` flag
tests/test_llm_prompts.py               # new tests for optional effectiveness_report kwarg
tests/test_llm_enhancer.py              # new tests for compute-once-pass-twice + graceful aggregation failure
tests/test_cli.py                       # new tests for stats subcommand
scripts/e2e_matrix.sh                   # append test_effectiveness.py to the existing LLM diagnosis block
```

**Unchanged:** `src/afteragent/store.py`, `src/afteragent/workflow.py`, `src/afteragent/diagnostics.py`, `src/afteragent/transcripts.py`, `src/afteragent/adapters.py`, `src/afteragent/capture.py`, `src/afteragent/ui.py`, `src/afteragent/models.py`, `src/afteragent/github.py`, `src/afteragent/config.py`, `pyproject.toml`, README.md, all other `src/afteragent/llm/*.py` files.

## File responsibilities

- **`effectiveness.py`** — owns the `EffectivenessMetric` and `EffectivenessReport` dataclasses, the `compute_effectiveness_metrics` entry point, the two private aggregator helpers (`_aggregate_finding_metrics`, `_aggregate_intervention_metrics`), the `_build_metrics` helper, and the two public formatters (`format_metrics_for_prompt`, `format_metrics_for_cli`). Pure data reduction with no side effects.
- **`llm/prompts.py`** — gains optional `effectiveness_report` parameter on both prompt builders. When provided, prepends the effectiveness block before the base context block. When `None`, behavior is unchanged from sub-project 2.
- **`llm/enhancer.py`** — calls `compute_effectiveness_metrics(store)` once before the findings call, wraps the call in try/except that falls back to `None`, threads the report into both prompt builder calls.
- **`cli.py`** — new `stats` subparser + dispatch branch that imports from `afteragent.effectiveness` lazily.

---

## Task 1: Build the effectiveness aggregator

**Files:**
- Create: `src/afteragent/effectiveness.py`
- Create: `tests/test_effectiveness.py`

Goal: the dataclasses + `compute_effectiveness_metrics` entry point + both aggregator helpers + the `_build_metrics` helper. No formatters yet — those are Task 2.

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_effectiveness.py`:

```python
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
        # Seed 5 valid replays.
        for i in range(5):
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
        # Should not raise. The corrupt row contributes nothing.
        assert report.total_replays == 5
        # The valid 4 replays still produce the code metric.
        metrics_for_x = [m for m in report.finding_metrics if m.key == "code_x"]
        assert len(metrics_for_x) == 1
        assert metrics_for_x[0].samples == 4


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
```

- [ ] **Step 1.2: Run to verify it fails**

Run: `python3 -m pytest tests/test_effectiveness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'afteragent.effectiveness'`.

- [ ] **Step 1.3: Implement the aggregator**

Create `src/afteragent/effectiveness.py`:

```python
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .models import now_utc
from .store import Store


@dataclass(slots=True, frozen=True)
class EffectivenessMetric:
    """One row of the effectiveness report — either a finding code or an
    intervention (type, target) pair."""
    key: str
    kind: str                 # "finding_code" | "intervention_type_target"
    source: str               # "rule" | "llm" | "mixed"
    samples: int
    successes: int
    success_rate: float


@dataclass(slots=True, frozen=True)
class EffectivenessReport:
    """Aggregated effectiveness metrics snapshot."""
    total_replays: int
    min_samples_threshold: int
    finding_metrics: list[EffectivenessMetric]
    intervention_metrics: list[EffectivenessMetric]
    generated_at: str


def compute_effectiveness_metrics(
    store: Store,
    min_samples: int = 5,
) -> EffectivenessReport:
    """Aggregate per-finding and per-(intervention_type, target) win rates
    across all recorded replays. Metrics with fewer than min_samples samples
    are omitted — they would be statistical noise at such small N.
    """
    replay_rows = store.list_all_replay_runs()

    finding_metrics = _aggregate_finding_metrics(replay_rows, store, min_samples)
    intervention_metrics = _aggregate_intervention_metrics(replay_rows, min_samples)

    # Sort by sample count descending, then rate descending, then key
    # alphabetically — deterministic, most-confident entries first.
    finding_metrics.sort(key=lambda m: (-m.samples, -m.success_rate, m.key))
    intervention_metrics.sort(key=lambda m: (-m.samples, -m.success_rate, m.key))

    return EffectivenessReport(
        total_replays=len(replay_rows),
        min_samples_threshold=min_samples,
        finding_metrics=finding_metrics,
        intervention_metrics=intervention_metrics,
        generated_at=now_utc(),
    )


def _aggregate_finding_metrics(
    replay_rows: list[sqlite3.Row],
    store: Store,
    min_samples: int,
) -> list[EffectivenessMetric]:
    sample_counts: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    source_tags: dict[str, set[str]] = {}

    for row in replay_rows:
        try:
            comparison = json.loads(row["comparison_json"])
        except (TypeError, ValueError):
            continue
        resolved_set = set(comparison.get("resolved_findings") or [])
        try:
            source_findings = store.get_diagnoses(row["source_run_id"])
        except Exception:
            continue
        for finding_row in source_findings:
            code = finding_row["code"]
            sample_counts[code] = sample_counts.get(code, 0) + 1
            if code in resolved_set:
                success_counts[code] = success_counts.get(code, 0) + 1
            source_tags.setdefault(code, set()).add(finding_row["source"])

    return _build_metrics(
        sample_counts,
        success_counts,
        source_tags,
        kind="finding_code",
        min_samples=min_samples,
    )


def _aggregate_intervention_metrics(
    replay_rows: list[sqlite3.Row],
    min_samples: int,
) -> list[EffectivenessMetric]:
    sample_counts: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    source_tags: dict[str, set[str]] = {}

    for row in replay_rows:
        try:
            comparison = json.loads(row["comparison_json"])
            manifest = json.loads(row["intervention_manifest_json"])
        except (TypeError, ValueError):
            continue
        improved = bool(comparison.get("improved"))
        pairs_seen: set[str] = set()
        interventions = manifest.get("interventions") or []
        if not isinstance(interventions, list):
            continue
        for intervention in interventions:
            if not isinstance(intervention, dict):
                continue
            itype = intervention.get("type")
            itarget = intervention.get("target")
            if not itype or not itarget:
                continue
            pair = f"{itype}/{itarget}"
            pairs_seen.add(pair)
            source_tags.setdefault(pair, set()).add(
                intervention.get("source") or "rule"
            )
        for pair in pairs_seen:
            sample_counts[pair] = sample_counts.get(pair, 0) + 1
            if improved:
                success_counts[pair] = success_counts.get(pair, 0) + 1

    return _build_metrics(
        sample_counts,
        success_counts,
        source_tags,
        kind="intervention_type_target",
        min_samples=min_samples,
    )


def _build_metrics(
    sample_counts: dict[str, int],
    success_counts: dict[str, int],
    source_tags: dict[str, set[str]],
    kind: str,
    min_samples: int,
) -> list[EffectivenessMetric]:
    metrics: list[EffectivenessMetric] = []
    for key, samples in sample_counts.items():
        if samples < min_samples:
            continue
        successes = success_counts.get(key, 0)
        tags = source_tags.get(key, set())
        if not tags:
            source = "mixed"
        elif len(tags) == 1:
            source = next(iter(tags))
        else:
            source = "mixed"
        metrics.append(
            EffectivenessMetric(
                key=key,
                kind=kind,
                source=source,
                samples=samples,
                successes=successes,
                success_rate=successes / samples if samples else 0.0,
            )
        )
    return metrics
```

- [ ] **Step 1.4: Run the tests**

Run: `python3 -m pytest tests/test_effectiveness.py -v`
Expected: PASS — all 12 tests pass.

Run the full suite:
Run: `python3 -m pytest -v`
Expected: 179 existing + 12 new = 191 tests, all pass.

- [ ] **Step 1.5: Commit**

```bash
git add src/afteragent/effectiveness.py tests/test_effectiveness.py
git commit -m "$(cat <<'EOF'
Add effectiveness aggregator module

New src/afteragent/effectiveness.py with EffectivenessMetric and
EffectivenessReport dataclasses plus compute_effectiveness_metrics
entry point. Aggregates per-finding-code resolution rates (Key 1)
and per-(intervention type, target) win rates (Key 2) across the
replay_runs corpus.

Handles corrupt replay rows, deleted source runs, and below-
threshold metrics gracefully. Source tagging distinguishes rule,
llm, and mixed origin for each metric. Deterministic sort by
(samples desc, rate desc, key).

Sub-project 3 task 1/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Build the formatters

**Files:**
- Modify: `src/afteragent/effectiveness.py` (append)
- Modify: `tests/test_effectiveness.py` (append)

Goal: `format_metrics_for_prompt` (prompt injection flavor, `findings` and `interventions` sections) and `format_metrics_for_cli` (terminal-friendly table).

- [ ] **Step 2.1: Write the failing tests**

Append to `tests/test_effectiveness.py`. The `format_metrics_for_prompt` and `format_metrics_for_cli` imports should be added to the existing top-of-file import block; do NOT create a mid-file import:

```python
from afteragent.effectiveness import (
    EffectivenessMetric,
    EffectivenessReport,
    compute_effectiveness_metrics,
    format_metrics_for_cli,
    format_metrics_for_prompt,
)
```

Then append these tests to the bottom of the file:

```python
def _build_report(
    total_replays: int = 5,
    min_samples_threshold: int = 5,
    finding_metrics: list[EffectivenessMetric] | None = None,
    intervention_metrics: list[EffectivenessMetric] | None = None,
) -> EffectivenessReport:
    return EffectivenessReport(
        total_replays=total_replays,
        min_samples_threshold=min_samples_threshold,
        finding_metrics=finding_metrics or [],
        intervention_metrics=intervention_metrics or [],
        generated_at="2026-04-11T12:00:00Z",
    )


def test_format_for_prompt_empty_report_returns_empty_string():
    report = _build_report(total_replays=0)
    assert format_metrics_for_prompt(report, section="findings") == ""
    assert format_metrics_for_prompt(report, section="interventions") == ""


def test_format_for_prompt_findings_section_shape():
    metrics = [
        EffectivenessMetric(
            key="low_diff_overlap",
            kind="finding_code",
            source="rule",
            samples=10,
            successes=8,
            success_rate=0.8,
        ),
    ]
    report = _build_report(total_replays=10, finding_metrics=metrics)
    output = format_metrics_for_prompt(report, section="findings")
    assert "## Historical effectiveness (finding codes)" in output
    assert "code=low_diff_overlap" in output
    assert "80%" in output
    assert "(8/10, source=rule)" in output
    assert "Based on 10 prior replays" in output


def test_format_for_prompt_interventions_section_shape():
    metrics = [
        EffectivenessMetric(
            key="prompt_patch/task_prompt",
            kind="intervention_type_target",
            source="mixed",
            samples=12,
            successes=9,
            success_rate=0.75,
        ),
    ]
    report = _build_report(total_replays=12, intervention_metrics=metrics)
    output = format_metrics_for_prompt(report, section="interventions")
    assert "## Historical effectiveness (intervention type/target)" in output
    assert "pair=prompt_patch/task_prompt" in output
    assert "75%" in output
    assert "source=mixed" in output


def test_format_for_prompt_caps_at_max_rows():
    metrics = [
        EffectivenessMetric(
            key=f"code_{i}",
            kind="finding_code",
            source="rule",
            samples=20 - i,
            successes=10,
            success_rate=10 / (20 - i),
        )
        for i in range(15)
    ]
    report = _build_report(total_replays=20, finding_metrics=metrics)
    output = format_metrics_for_prompt(report, section="findings")
    # Only the first 10 metrics should appear.
    assert "code=code_0" in output
    assert "code=code_9" in output
    assert "code=code_10" not in output
    assert "code=code_14" not in output


def test_format_for_prompt_rejects_unknown_section_name():
    report = _build_report()
    with pytest.raises(ValueError, match="Unknown section"):
        format_metrics_for_prompt(report, section="garbage")


def test_format_for_cli_empty_store():
    report = _build_report(total_replays=0)
    output = format_metrics_for_cli(report)
    assert "No replays recorded yet." in output
    assert "0 total replays" in output


def test_format_for_cli_below_threshold():
    report = _build_report(total_replays=3)  # metrics lists empty
    output = format_metrics_for_cli(report)
    assert "3 total replays" in output
    assert "(no codes with ≥5 samples)" in output
    assert "(no pairs with ≥5 samples)" in output


def test_format_for_cli_populated():
    finding_metrics = [
        EffectivenessMetric(
            key="low_diff_overlap_with_failing_files",
            kind="finding_code",
            source="mixed",
            samples=27,
            successes=21,
            success_rate=21 / 27,
        ),
    ]
    intervention_metrics = [
        EffectivenessMetric(
            key="prompt_patch/task_prompt",
            kind="intervention_type_target",
            source="rule",
            samples=25,
            successes=18,
            success_rate=18 / 25,
        ),
    ]
    report = _build_report(
        total_replays=27,
        finding_metrics=finding_metrics,
        intervention_metrics=intervention_metrics,
    )
    output = format_metrics_for_cli(report)
    assert "AfterAgent effectiveness (27 total replays" in output
    assert "Finding code resolution rates:" in output
    assert "low_diff_overlap_with_failing_files" in output
    assert "78%" in output
    assert "(21/27, source=mixed)" in output
    assert "Intervention (type/target) win rates:" in output
    assert "prompt_patch/task_prompt" in output
    assert "72%" in output
    assert "(18/25, source=rule)" in output
```

- [ ] **Step 2.2: Run to verify fails**

Run: `python3 -m pytest tests/test_effectiveness.py -v`
Expected: FAIL — `ImportError` on `format_metrics_for_prompt` and `format_metrics_for_cli`.

- [ ] **Step 2.3: Implement both formatters**

Append to `src/afteragent/effectiveness.py`:

```python
_PROMPT_MAX_ROWS = 10


def format_metrics_for_prompt(
    report: EffectivenessReport,
    section: str,
) -> str:
    """Compact text block for prompt injection. Returns an empty string when
    no metrics qualify so callers can skip the section cleanly.
    """
    if section == "findings":
        metrics = report.finding_metrics
        header = "## Historical effectiveness (finding codes)"
        row_label = "code"
    elif section == "interventions":
        metrics = report.intervention_metrics
        header = "## Historical effectiveness (intervention type/target)"
        row_label = "pair"
    else:
        raise ValueError(f"Unknown section: {section!r}")

    if not metrics:
        return ""

    top = metrics[:_PROMPT_MAX_ROWS]
    lines = [
        header,
        "",
        f"Based on {report.total_replays} prior replays. Only entries with "
        f"{report.min_samples_threshold}+ samples are shown.",
        "",
    ]
    for m in top:
        pct = int(round(m.success_rate * 100))
        lines.append(
            f"- {row_label}={m.key} — {pct}% "
            f"({m.successes}/{m.samples}, source={m.source})"
        )
    lines.append("")
    lines.append(
        "Use this data as evidence when deciding whether to confirm, reject, "
        "or supplement rule-based findings. Low historical rates suggest the "
        "code or intervention pattern is unreliable — consider alternatives."
    )
    return "\n".join(lines)


def format_metrics_for_cli(report: EffectivenessReport) -> str:
    """Terminal-friendly table."""
    lines = [
        f"AfterAgent effectiveness ({report.total_replays} total replays, "
        f"min_samples={report.min_samples_threshold})",
        "",
    ]

    if report.total_replays == 0:
        lines.append("No replays recorded yet.")
        return "\n".join(lines)

    lines.append("Finding code resolution rates:")
    if not report.finding_metrics:
        lines.append(f"  (no codes with ≥{report.min_samples_threshold} samples)")
    else:
        for m in report.finding_metrics:
            pct = int(round(m.success_rate * 100))
            lines.append(
                f"  {m.key:<48s} {pct:>3d}% "
                f"({m.successes}/{m.samples}, source={m.source})"
            )

    lines.append("")
    lines.append("Intervention (type/target) win rates:")
    if not report.intervention_metrics:
        lines.append(f"  (no pairs with ≥{report.min_samples_threshold} samples)")
    else:
        for m in report.intervention_metrics:
            pct = int(round(m.success_rate * 100))
            lines.append(
                f"  {m.key:<48s} {pct:>3d}% "
                f"({m.successes}/{m.samples}, source={m.source})"
            )

    return "\n".join(lines)
```

- [ ] **Step 2.4: Run tests**

Run: `python3 -m pytest tests/test_effectiveness.py -v`
Expected: PASS — 12 aggregator tests + 8 formatter tests = 20 tests.

Run: `python3 -m pytest -v`
Expected: 179 + 20 = 199 pass.

- [ ] **Step 2.5: Commit**

```bash
git add src/afteragent/effectiveness.py tests/test_effectiveness.py
git commit -m "$(cat <<'EOF'
Add effectiveness formatters for prompt and CLI

format_metrics_for_prompt emits a ~250-token block suitable for
injection into the LLM prompts, with separate flavors for the
findings call and the interventions call. Caps at 10 rows of the
highest-sample metrics. Returns empty string for empty reports
so callers can skip the section cleanly.

format_metrics_for_cli emits a terminal-friendly table with
padded columns, handles empty-store / below-threshold / populated
branches explicitly.

Sub-project 3 task 2/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Prompt builder integration

**Files:**
- Modify: `src/afteragent/llm/prompts.py`
- Modify: `tests/test_llm_prompts.py`

Goal: both prompt builders (`build_diagnosis_prompt`, `build_interventions_prompt`) accept an optional `effectiveness_report` parameter. When provided, prepend the effectiveness block before the base context block.

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/test_llm_prompts.py`. Add imports at the top of the file:

```python
from afteragent.effectiveness import EffectivenessMetric, EffectivenessReport
```

Then append tests at the bottom:

```python
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
```

- [ ] **Step 3.2: Run to verify fails**

Run: `python3 -m pytest tests/test_llm_prompts.py -v -k "effectiveness"`
Expected: FAIL — `build_diagnosis_prompt` doesn't accept `effectiveness_report` kwarg yet.

- [ ] **Step 3.3: Update the prompt builders**

In `src/afteragent/llm/prompts.py`:

1. Add an import at the top (after the existing imports):

```python
from ..effectiveness import EffectivenessReport, format_metrics_for_prompt
```

2. Update `build_diagnosis_prompt`:

```python
def build_diagnosis_prompt(
    context: DiagnosisContext,
    effectiveness_report: EffectivenessReport | None = None,
) -> tuple[str, str]:
    """Build (system, user) strings for the findings call."""
    effectiveness_block = ""
    if effectiveness_report is not None:
        effectiveness_block = format_metrics_for_prompt(
            effectiveness_report, section="findings"
        )
    base = _build_base_context_block(
        context, include_findings_header="Rule-based findings"
    )
    user = f"{effectiveness_block}\n\n{base}" if effectiveness_block else base
    user = _enforce_token_budget(user, context)
    return (_DIAGNOSIS_SYSTEM_PROMPT, user)
```

3. Update `build_interventions_prompt`:

```python
def build_interventions_prompt(
    context: DiagnosisContext,
    merged_findings: list[MergedFinding],
    effectiveness_report: EffectivenessReport | None = None,
) -> tuple[str, str]:
    """Build (system, user) strings for the interventions call."""
    effectiveness_block = ""
    if effectiveness_report is not None:
        effectiveness_block = format_metrics_for_prompt(
            effectiveness_report, section="interventions"
        )

    base = _build_base_context_block(context, include_findings_header=None)

    if merged_findings:
        findings_section = "## Confirmed findings to address\n\n" + json.dumps(
            [
                {
                    "code": f.code,
                    "title": f.title,
                    "severity": f.severity,
                    "summary": f.summary,
                    "evidence": f.evidence,
                    "source": f.source,
                }
                for f in merged_findings
            ],
            indent=2,
        )
    else:
        findings_section = "## Confirmed findings to address\n\n(none)"

    if effectiveness_block:
        user = f"{effectiveness_block}\n\n{findings_section}\n\n{base}"
    else:
        user = f"{findings_section}\n\n{base}"
    user = _enforce_token_budget(user, context)
    return (_INTERVENTIONS_SYSTEM_PROMPT, user)
```

**Important:** the existing body of `build_interventions_prompt` constructs the `findings_section` string — preserve that logic. Only the prefix assembly at the end changes to include the optional `effectiveness_block`.

- [ ] **Step 3.4: Run tests**

Run: `python3 -m pytest tests/test_llm_prompts.py -v`
Expected: PASS — existing prompt tests + 5 new = all pass.

Run full suite:
Run: `python3 -m pytest -v`
Expected: 199 + 5 = 204 pass.

- [ ] **Step 3.5: Commit**

```bash
git add src/afteragent/llm/prompts.py tests/test_llm_prompts.py
git commit -m "$(cat <<'EOF'
Thread effectiveness_report through LLM prompt builders

build_diagnosis_prompt and build_interventions_prompt gain an
optional effectiveness_report parameter. When provided, prepend
the effectiveness block (via format_metrics_for_prompt) before
the existing context block. When None (default), behavior is
identical to sub-project 2.

The findings call gets the findings-flavor block (per-code
resolution rates). The interventions call gets the interventions-
flavor block (per-(type, target) win rates). Existing token
budget enforcement is unchanged.

Sub-project 3 task 3/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Enhancer integration

**Files:**
- Modify: `src/afteragent/llm/enhancer.py`
- Modify: `tests/test_llm_enhancer.py`

Goal: `enhance_diagnosis_with_llm` computes `EffectivenessReport` once before the findings call and threads it through both prompt builders. Wrapped in try/except that falls back to `None` on any failure.

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/test_llm_enhancer.py`. Add to the top-of-file imports:

```python
from unittest.mock import patch
```

Then append these tests:

```python
def test_enhance_computes_effectiveness_report_and_passes_to_both_prompts(tmp_path):
    """The enhancer should call compute_effectiveness_metrics ONCE and pass
    the result into both prompt builder calls."""
    store = _make_store(tmp_path)
    _seed_minimal_run(store)

    client = StubClient(responses={
        "report_findings": _success_findings_response([]),
        "author_interventions": _success_interventions_response([]),
    })

    with patch(
        "afteragent.llm.enhancer.compute_effectiveness_metrics"
    ) as mock_compute, patch(
        "afteragent.llm.enhancer.build_diagnosis_prompt",
        wraps=None,
    ) as mock_diag, patch(
        "afteragent.llm.enhancer.build_interventions_prompt",
        wraps=None,
    ) as mock_inter:
        # Make the computed report a simple sentinel object.
        sentinel_report = object()
        mock_compute.return_value = sentinel_report

        # Let the real prompt builders run so the rest of the pipeline works.
        from afteragent.llm.prompts import (
            build_diagnosis_prompt as real_build_diagnosis_prompt,
            build_interventions_prompt as real_build_interventions_prompt,
        )
        mock_diag.side_effect = real_build_diagnosis_prompt
        mock_inter.side_effect = real_build_interventions_prompt

        enhance_diagnosis_with_llm(store, "run1", client, _make_config())

        # compute_effectiveness_metrics was called exactly once.
        assert mock_compute.call_count == 1

        # Both prompt builders were called with effectiveness_report=sentinel.
        assert mock_diag.call_count == 1
        diag_call_kwargs = mock_diag.call_args.kwargs
        assert diag_call_kwargs.get("effectiveness_report") is sentinel_report

        assert mock_inter.call_count == 1
        inter_call_kwargs = mock_inter.call_args.kwargs
        assert inter_call_kwargs.get("effectiveness_report") is sentinel_report


def test_enhance_tolerates_effectiveness_computation_failure(tmp_path):
    """If compute_effectiveness_metrics raises, the enhancer should still
    complete successfully with effectiveness_report=None passed to both
    prompt builders."""
    store = _make_store(tmp_path)
    _seed_minimal_run(store)

    client = StubClient(responses={
        "report_findings": _success_findings_response([]),
        "author_interventions": _success_interventions_response([]),
    })

    with patch(
        "afteragent.llm.enhancer.compute_effectiveness_metrics",
        side_effect=RuntimeError("simulated aggregation failure"),
    ), patch(
        "afteragent.llm.enhancer.build_diagnosis_prompt",
    ) as mock_diag, patch(
        "afteragent.llm.enhancer.build_interventions_prompt",
    ) as mock_inter:
        from afteragent.llm.prompts import (
            build_diagnosis_prompt as real_build_diagnosis_prompt,
            build_interventions_prompt as real_build_interventions_prompt,
        )
        mock_diag.side_effect = real_build_diagnosis_prompt
        mock_inter.side_effect = real_build_interventions_prompt

        result = enhance_diagnosis_with_llm(store, "run1", client, _make_config())

        # Enhancer completed successfully, not an error.
        assert result.status in ("success", "partial")

        # Both prompt builders got effectiveness_report=None.
        assert mock_diag.call_args.kwargs.get("effectiveness_report") is None
        assert mock_inter.call_args.kwargs.get("effectiveness_report") is None
```

- [ ] **Step 4.2: Run to verify fails**

Run: `python3 -m pytest tests/test_llm_enhancer.py -v -k "effectiveness"`
Expected: FAIL — `compute_effectiveness_metrics` not yet imported in enhancer, `effectiveness_report` not passed to prompt builders.

- [ ] **Step 4.3: Update the enhancer**

In `src/afteragent/llm/enhancer.py`:

1. Add an import at the top:

```python
from ..effectiveness import compute_effectiveness_metrics
```

2. In `enhance_diagnosis_with_llm`, immediately after `context = load_diagnosis_context(store, run_id)` and before the findings call, add:

```python
    # Compute the effectiveness report once. If it fails, fall back cleanly.
    try:
        effectiveness_report = compute_effectiveness_metrics(store)
    except Exception:
        effectiveness_report = None
```

3. Update the `build_diagnosis_prompt` call to pass the new kwarg:

```python
    system, user = build_diagnosis_prompt(
        context,
        effectiveness_report=effectiveness_report,
    )
```

4. Update the `build_interventions_prompt` call to pass the new kwarg:

```python
    system, user = build_interventions_prompt(
        context,
        merged,
        effectiveness_report=effectiveness_report,
    )
```

**Important:** do not change any other line in `enhance_diagnosis_with_llm`. The findings merge logic, the persistence calls, and the error handling stay exactly as sub-project 2 left them.

- [ ] **Step 4.4: Run tests**

Run: `python3 -m pytest tests/test_llm_enhancer.py -v`
Expected: PASS — existing enhancer tests + 2 new = all pass.

Run full suite:
Run: `python3 -m pytest -v`
Expected: 204 + 2 = 206 pass.

- [ ] **Step 4.5: Commit**

```bash
git add src/afteragent/llm/enhancer.py tests/test_llm_enhancer.py
git commit -m "$(cat <<'EOF'
Thread effectiveness_report through enhance_diagnosis_with_llm

The enhancer now calls compute_effectiveness_metrics once before
the findings LLM call, then passes the result into both the
findings prompt and the interventions prompt. Wrapped in try/
except that falls back to effectiveness_report=None if
aggregation fails — the run still enhances using sub-project 2's
behavior as the fallback path.

Sub-project 3 task 4/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: CLI `stats` subcommand

**Files:**
- Modify: `src/afteragent/cli.py`
- Modify: `tests/test_cli.py`

Goal: new `afteragent stats` subcommand prints the effectiveness report via `format_metrics_for_cli`. Accepts `--min-samples` flag.

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_stats_subcommand_empty_store(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from afteragent.cli import main

    exit_code = main(["stats"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No replays recorded yet." in captured.out
    assert "0 total replays" in captured.out


def test_stats_subcommand_populated_store(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())

    # Seed 5 replays with the same finding code, all resolved.
    for i in range(5):
        source_id = f"src{i}"
        replay_id = f"rep{i}"
        store.create_run(source_id, "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
        store.replace_diagnosis(
            source_id,
            [{
                "run_id": source_id,
                "code": "low_diff_overlap",
                "title": "x",
                "severity": "medium",
                "summary": "x",
                "evidence_json": "[]",
            }],
            [],
        )
        store.create_run(replay_id, "echo replay", str(tmp_path), "2026-04-10T13:00:00Z")
        store.finish_run(replay_id, "passed", 0, "2026-04-10T13:00:01Z", 1000, summary="ok")
        store.save_intervention_set(
            set_id=f"iset{i}",
            source_run_id=source_id,
            version=1,
            kind="export",
            created_at="2026-04-10T13:00:00Z",
            output_dir="/tmp/fake",
            manifest={"interventions": []},
        )
        store.record_replay_run(
            source_run_id=source_id,
            replay_run_id=replay_id,
            intervention_set_id=f"iset{i}",
            created_at="2026-04-10T13:00:00Z",
            applied_before_replay=True,
            comparison={"resolved_findings": ["low_diff_overlap"], "improved": True, "score": 10},
        )

    from afteragent.cli import main
    exit_code = main(["stats"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "5 total replays" in captured.out
    assert "Finding code resolution rates:" in captured.out
    assert "low_diff_overlap" in captured.out
    assert "100%" in captured.out


def test_stats_subcommand_accepts_min_samples_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())

    # Seed 3 replays — below default threshold 5.
    for i in range(3):
        source_id = f"src{i}"
        replay_id = f"rep{i}"
        store.create_run(source_id, "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
        store.replace_diagnosis(
            source_id,
            [{
                "run_id": source_id,
                "code": "code_x",
                "title": "x",
                "severity": "medium",
                "summary": "x",
                "evidence_json": "[]",
            }],
            [],
        )
        store.create_run(replay_id, "echo replay", str(tmp_path), "2026-04-10T13:00:00Z")
        store.finish_run(replay_id, "passed", 0, "2026-04-10T13:00:01Z", 1000, summary="ok")
        store.save_intervention_set(
            set_id=f"iset{i}",
            source_run_id=source_id,
            version=1,
            kind="export",
            created_at="2026-04-10T13:00:00Z",
            output_dir="/tmp/fake",
            manifest={"interventions": []},
        )
        store.record_replay_run(
            source_run_id=source_id,
            replay_run_id=replay_id,
            intervention_set_id=f"iset{i}",
            created_at="2026-04-10T13:00:00Z",
            applied_before_replay=True,
            comparison={"resolved_findings": ["code_x"], "improved": True, "score": 10},
        )

    from afteragent.cli import main

    # With default min_samples=5, metric is below threshold → not shown.
    exit_code = main(["stats"])
    captured_default = capsys.readouterr()
    assert "code_x" not in captured_default.out

    # With --min-samples 3, metric is shown.
    exit_code = main(["stats", "--min-samples", "3"])
    captured_low = capsys.readouterr()
    assert "code_x" in captured_low.out
```

- [ ] **Step 5.2: Run to verify fails**

Run: `python3 -m pytest tests/test_cli.py -v -k "stats"`
Expected: FAIL — `stats` subcommand doesn't exist yet.

- [ ] **Step 5.3: Add the subcommand to `cli.py`**

In `src/afteragent/cli.py`:

1. In `build_parser`, after the existing `enhance_parser` block (added in sub-project 2), add the new subparser:

```python
    stats_parser = subparsers.add_parser(
        "stats",
        help="Show effectiveness metrics aggregated from replay history",
    )
    stats_parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum samples required before a metric is included (default: 5)",
    )
```

2. In `main`, alongside the other `if args.command == "..."` dispatch blocks, add:

```python
    if args.command == "stats":
        from .effectiveness import (
            compute_effectiveness_metrics,
            format_metrics_for_cli,
        )
        report = compute_effectiveness_metrics(store, min_samples=args.min_samples)
        print(format_metrics_for_cli(report))
        return 0
```

The import is lazy (inside the dispatch branch) so existing CLI paths don't pay the import cost on every invocation. Consistent with the pattern used by the `enhance` subcommand's dispatch.

- [ ] **Step 5.4: Run tests**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected: PASS — existing CLI tests + 3 new = all pass.

Run full suite:
Run: `python3 -m pytest -v`
Expected: 206 + 3 = 209 pass.

- [ ] **Step 5.5: Commit**

```bash
git add src/afteragent/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
Add `afteragent stats` subcommand

New CLI entry point for directly inspecting the effectiveness
aggregator output. Accepts --min-samples to override the default
5-sample threshold. Prints a terminal-friendly table via
format_metrics_for_cli. Import is lazy to keep other CLI paths
fast.

Sub-project 3 task 5/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: E2E matrix update

**Files:**
- Modify: `scripts/e2e_matrix.sh`

Goal: append `tests/test_effectiveness.py` to the existing LLM diagnosis block so the matrix run includes sub-project 3's tests.

- [ ] **Step 6.1: Inspect the current matrix**

Run: `cat scripts/e2e_matrix.sh`
Find the existing `Running LLM diagnosis tests...` block. It was added in sub-project 2 task 13 and runs a pytest command against the llm-related test files.

- [ ] **Step 6.2: Add `test_effectiveness.py` to the block**

Edit `scripts/e2e_matrix.sh`. Find the pytest invocation inside the `Running LLM diagnosis tests...` block and add `tests/test_effectiveness.py` to the list of files. The block should look like:

```bash
echo
echo "Running LLM diagnosis tests..."
python3 -m pytest -v \
    tests/test_llm_config.py \
    tests/test_llm_cost_table.py \
    tests/test_llm_client.py \
    tests/test_llm_schemas.py \
    tests/test_llm_prompts.py \
    tests/test_llm_merge.py \
    tests/test_llm_enhancer.py \
    tests/test_store_llm.py \
    tests/test_diagnostics_llm_hook.py \
    tests/test_effectiveness.py
```

The exact list may differ from the above — preserve whatever sub-project 2 left; just add the new file to the end.

- [ ] **Step 6.3: Run the matrix**

Run: `bash scripts/e2e_matrix.sh`
Expected: all blocks pass. The LLM diagnosis block count should increase by the 20 effectiveness tests.

- [ ] **Step 6.4: Commit**

```bash
git add scripts/e2e_matrix.sh
git commit -m "$(cat <<'EOF'
Add test_effectiveness.py to e2e matrix LLM block

Sub-project 3's effectiveness tests now run as part of the LLM
diagnosis pytest block in scripts/e2e_matrix.sh, alongside the
sub-project 2 test files. No new block — same integration surface.

Sub-project 3 task 6/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Manual dogfood acceptance check

**Files:** none (manual verification)

This is the quality-bar acceptance step from the spec's success criterion #8. It requires real replay data in a `.afteragent/` store AND real LLM credentials. If either is unavailable, the task is marked as "structural verification complete, quality inspection pending" (same pattern as sub-project 2 task 14).

- [ ] **Step 7.1: Confirm a provider is configured**

Set at least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` in the environment, OR install Ollama locally and pull a schema-capable model (`qwen2.5-coder:7b` recommended).

If no credentials are available, skip to Step 7.6 and document this as "pending user verification."

- [ ] **Step 7.2: Confirm at least 5 replays exist in the store**

Run: `sqlite3 .afteragent/afteragent.sqlite3 "SELECT COUNT(*) FROM replay_runs;"`

If the count is below 5, either:
- **Use an existing project's `.afteragent/` store** — e.g., `cd /some/project && afteragent stats`
- **Seed synthetic replays** via `afteragent attempt-repair` or the `replay` command on existing captured runs
- **Document as structurally verified only** — skip to Step 7.6 and note that quality acceptance is pending real replay data

If ≥5 replays exist, continue.

- [ ] **Step 7.3: Run `afteragent stats` and eyeball the output**

Run: `afteragent stats`

Expected output shape:
```
AfterAgent effectiveness (N total replays, min_samples=5)

Finding code resolution rates:
  <code_1>                                          XX% (Y/Z, source=...)
  ...

Intervention (type/target) win rates:
  <pair_1>                                          XX% (Y/Z, source=...)
  ...
```

Verify:
- The total_replays number matches `SELECT COUNT(*) FROM replay_runs;`
- Finding codes that appear in your source runs show up in the output (assuming they have ≥5 samples)
- Intervention (type, target) pairs you've actually used show up (same threshold)
- Numbers look sane — no 200% win rates, no negative samples

- [ ] **Step 7.4: Run `afteragent enhance <run-id>` with a real provider**

Pick a run ID from `afteragent runs` that has interesting findings. Run:

```bash
afteragent enhance <run-id>
```

Expected: the CLI prints `Enhanced run <id>: +N findings, M intervention(s) (...)` with a non-zero token count.

- [ ] **Step 7.5: Confirm the effectiveness block reached the LLM**

If the provider supports verbose logging or you can inspect the request, confirm the user prompt contained the `## Historical effectiveness` block. Otherwise, indirect confirmation:

- The LLM-authored findings should reference specific codes from the effectiveness block (e.g., rejecting a code with a low historical rate)
- The LLM-authored interventions should reference the (type, target) win rates if they influenced the choice

If the outputs look the same as sub-project 2's (i.e., no visible effect from the effectiveness data), the feedback loop technically works but may need prompt tuning — document as a known-but-not-blocking observation.

- [ ] **Step 7.6: Record the result**

If steps 7.1 through 7.5 all passed, sub-project 3 is fully verified:

```bash
git tag subproject-3-complete
```

If credentials or replay data weren't available, document it here in the plan:
- Structural verification: COMPLETE (all 209 tests passing, unit + integration + CLI coverage)
- Quality verification: PENDING user-side dogfood with real credentials and real replay corpus

Move on to `superpowers:finishing-a-development-branch`.

---

## Self-review checklist (plan author)

**Spec coverage:**
- [x] Goal 1 (aggregator module producing EffectivenessReport with Key 1 + Key 2): Task 1
- [x] Goal 2 (soft feedback via prompt injection in both builders): Task 3
- [x] Goal 3 (compute once, thread through both calls, graceful fallback): Task 4
- [x] Goal 4 (`afteragent stats` CLI with `--min-samples`): Task 5
- [x] Goal 5 (never-break-the-run contract via try/except in enhancer): Task 4
- [x] Goal 6 (no schema changes): confirmed — no task modifies `store.py`
- [x] Non-goals respected: no hard filter (not touched), no auto-disable (not touched), no Key 3 (not touched), no UI changes (not touched), no caching (computed fresh each call), no per-repo scoping (global only), no time decay (samples counted equally)
- [x] All success criteria 1–7 map to tests in Tasks 1–5; criterion #8 is Task 7's manual acceptance step

**Placeholder scan:**
- No "TBD" / "TODO" / "similar to Task N".
- Every test has full Python code.
- Every implementation step has full Python code.
- Every commit message is fully written.

**Type consistency:**
- `EffectivenessMetric` field names match between Task 1 (definition), Task 2 (formatter reads), Task 3 (prompt test), Task 4 (enhancer test), Task 5 (CLI test).
- `EffectivenessReport` field names match across all tasks.
- `compute_effectiveness_metrics(store, min_samples=5)` signature matches Tasks 1, 4, 5.
- `format_metrics_for_prompt(report, section)` signature matches Tasks 2, 3.
- `format_metrics_for_cli(report)` signature matches Tasks 2, 5.
- `build_diagnosis_prompt(context, effectiveness_report=None)` matches Tasks 3, 4.
- `build_interventions_prompt(context, merged_findings, effectiveness_report=None)` matches Tasks 3, 4.

**Known plan imperfections (acknowledged, not blocking):**
- Task 7 requires real LLM credentials + real replay data; if either is unavailable, the acceptance step degrades to "structural verification only."
- The existing `test_llm_enhancer.py` uses `StubClient` which I'm extending via `patch()` — the stub pattern from sub-project 2 is preserved. No rewrites to existing tests.
- The existing `test_llm_prompts.py` uses `_seed_run_with_artifacts` helper from Task 7 of sub-project 2 — this plan re-uses that helper. No redefinition.
