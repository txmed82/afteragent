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
    # total_seen: every time a code appeared in source diagnoses (including
    # rows with corrupt comparison_json). Used only for the threshold guard so
    # that one corrupt row cannot suppress a metric that otherwise meets the bar.
    total_seen: dict[str, int] = {}
    # valid_samples / success_counts: only rows where comparison parsed cleanly.
    valid_samples: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    source_tags: dict[str, set[str]] = {}

    for row in replay_rows:
        try:
            comparison = json.loads(row["comparison_json"])
            resolved_set: set[str] | None = set(comparison.get("resolved_findings") or [])
        except (TypeError, ValueError):
            resolved_set = None  # corrupt — skip resolution counting but keep seen count

        try:
            source_findings = store.get_diagnoses(row["source_run_id"])
        except Exception:
            continue

        for finding_row in source_findings:
            code = finding_row["code"]
            total_seen[code] = total_seen.get(code, 0) + 1
            source_tags.setdefault(code, set()).add(finding_row["source"])
            if resolved_set is not None:
                valid_samples[code] = valid_samples.get(code, 0) + 1
                if code in resolved_set:
                    success_counts[code] = success_counts.get(code, 0) + 1

    return _build_metrics(
        total_seen=total_seen,
        valid_samples=valid_samples,
        success_counts=success_counts,
        source_tags=source_tags,
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

    # For intervention metrics, sample_counts == total_seen (no corrupt-row split needed).
    return _build_metrics(
        total_seen=sample_counts,
        valid_samples=sample_counts,
        success_counts=success_counts,
        source_tags=source_tags,
        kind="intervention_type_target",
        min_samples=min_samples,
    )


def _build_metrics(
    total_seen: dict[str, int],
    valid_samples: dict[str, int],
    success_counts: dict[str, int],
    source_tags: dict[str, set[str]],
    kind: str,
    min_samples: int,
) -> list[EffectivenessMetric]:
    """Build metrics list.

    The threshold guard uses *total_seen* so that a single corrupt replay row
    cannot suppress a metric that has otherwise accumulated enough evidence.
    The reported *samples* value reflects only the rows where the comparison
    payload parsed successfully (valid_samples), keeping the rate calculation
    accurate.
    """
    metrics: list[EffectivenessMetric] = []
    for key, seen in total_seen.items():
        if seen < min_samples:
            continue
        samples = valid_samples.get(key, 0)
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
