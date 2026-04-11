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
        lines.append(f"  (no codes with \u2265{report.min_samples_threshold} samples)")
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
        lines.append(f"  (no pairs with \u2265{report.min_samples_threshold} samples)")
    else:
        for m in report.intervention_metrics:
            pct = int(round(m.success_rate * 100))
            lines.append(
                f"  {m.key:<48s} {pct:>3d}% "
                f"({m.successes}/{m.samples}, source={m.source})"
            )

    return "\n".join(lines)
