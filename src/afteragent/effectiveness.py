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
    """Build metrics list, filtering out any key with fewer than min_samples."""
    metrics: list[EffectivenessMetric] = []
    for key, samples in sample_counts.items():
        if samples < min_samples:
            continue
        successes = success_counts.get(key, 0)
        tags = source_tags.get(key, set())
        if not tags or len(tags) > 1:
            source = "mixed"
        else:
            source = next(iter(tags))
        rate = successes / samples if samples else 0.0
        metrics.append(
            EffectivenessMetric(
                key=key,
                kind=kind,
                source=source,
                samples=samples,
                successes=successes,
                success_rate=rate,
            )
        )
    return metrics
