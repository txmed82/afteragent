# Sub-Project 3: Effectiveness-Driven Feedback Loop — Design

**Status:** Design approved, pending spec review
**Date:** 2026-04-11
**Owner:** Colin
**Scope:** Sub-project 3 of 5 in the AfterAgent self-improvement arc.
**Depends on:** Sub-projects 1 (transcript ingestion) and 2 (LLM-driven diagnosis), both shipped in v0.2.0.

---

## Context

Sub-project 2 added an LLM layer that reviews and augments the rule-based detectors and authors interventions per run. The system now writes findings and interventions tagged `source='rule'` or `source='llm'`, records per-call token counts and cost in a new `llm_generations` table, and persists per-replay comparison data (`resolved_findings`, `new_findings`, `score`, `improved`) in the existing `replay_runs` table.

What sub-project 2 did **not** do: feed any of that historical data back into the LLM pipeline. Every `afteragent enhance` call runs blind — the LLM never knows which finding codes or intervention types have actually led to improved replays in the past. That's the gap sub-project 3 closes.

Sub-project 3 is the **learning loop** stage of the self-improvement arc. It aggregates historical effectiveness data from the replay corpus, surfaces it as soft feedback in the LLM prompts, and adds a CLI surface for direct inspection. The goal: LLM-authored findings and interventions become more reliable over time because the LLM sees which patterns historically work and which don't.

Sub-project 3 does **not**:
- Hard-filter LLM output based on win rate thresholds (deferred to sub-project 3.5 once calibration data exists)
- Auto-disable rule detectors based on runtime data (conflates "rule is wrong" with "intervention is wrong" — different problems)
- Change the replay scoring or per-replay comparison logic (that's `compare_runs` in `workflow.py`, untouched)
- Surface effectiveness data in the browser UI (sub-project 5's territory)
- Snapshot findings into the replay manifest at replay time (schema change, deferred)

Sub-projects 4 (broaden past PR repair) and 5 (narrative UI) will both consume the effectiveness module built here.

## Goals

1. Build a single-purpose `src/afteragent/effectiveness.py` module that reads historical replay data from the existing `replay_runs` table and produces an `EffectivenessReport` with two metric families: per-finding-code resolution rates (Key 1) and per-(intervention_type, target) win rates (Key 2).
2. Surface that report as soft feedback in both LLM prompts (diagnosis and interventions) via an optional `effectiveness_report` parameter on `build_diagnosis_prompt` and `build_interventions_prompt`. The LLM sees historical rates as context for its confirm/reject/novel decisions and its intervention choices.
3. Compute the report once per `enhance_diagnosis_with_llm` call and thread it through to both prompt builders. Graceful fallback to the sub-project 2 shape (no effectiveness block) if aggregation fails.
4. Add a new `afteragent stats` CLI subcommand for direct terminal inspection of the effectiveness data. Supports a `--min-samples` flag to control the threshold below which metrics are hidden.
5. Preserve the never-break-the-run contract from prior sub-projects: every failure mode in sub-project 3 degrades to "run as if sub-project 3 didn't exist." Rule-based diagnosis, sub-project 2's LLM enhancement, and the existing UI all continue working if the effectiveness module itself is broken.
6. No schema changes. The `replay_runs` table already contains everything the aggregator needs: `comparison_json` is stored directly on each row, and the intervention manifest is obtained by joining `replay_runs.intervention_set_id → intervention_sets.manifest_json` (see `_aggregate_intervention_metrics` in the implementation sketches below). Sub-project 3 is purely additive over the existing schema.

## Non-goals

- **No hard-filter pruning of LLM output.** Option C from the scope brainstorm (strip findings/interventions with catastrophic win rates before persisting) is deferred. Soft feedback only in this sub-project.
- **No auto-disable of rule detectors.** The 6 rule-based detectors in `diagnostics.py` are untouched. Even if a rule code has 0% historical resolution, the rule still fires — the LLM uses the effectiveness data to decide whether to reject it via sub-project 2's `origin='rejected_rule'` mechanism.
- **No Key 3 (finding code × intervention type pair).** Deferred until Keys 1 + 2 produce actionable data and we see whether the additional granularity matters.
- **No UI changes.** The browser view is untouched. Effectiveness is CLI-only in sub-project 3.
- **No caching or persistence of the effectiveness report.** Computed fresh on each invocation. At realistic replay volumes (low hundreds) this is milliseconds.
- **No snapshot of findings into the replay manifest at replay creation time.** The correct fix for the "source findings may have been overwritten since the replay" imprecision is a schema change — deferred to a follow-up.
- **No per-repo or per-PR scoping.** Effectiveness is computed globally across all replays in the store.
- **No time decay or recency weighting.** A replay from 6 months ago counts the same as one from yesterday.
- **No statistical significance display.** The CLI prints raw `N/M` counts; users eyeball sample size themselves.
- **No changes to `compare_runs`, `replay_run`, `export_interventions`, `apply_interventions`, or any other `workflow.py` function.** Per-replay computation is sub-project 1/2 territory.
- **No changes to any sub-project 1 or 2 file not explicitly listed in the "Files touched" section below.**

## Architecture

### New module: `src/afteragent/effectiveness.py`

One file, three responsibilities:

1. **Aggregation.** `compute_effectiveness_metrics(store, min_samples=5) -> EffectivenessReport` walks the `replay_runs` table, parses each row's `comparison_json` and `intervention_manifest_json`, and produces metrics keyed by finding code and by `(intervention_type, target)` pair. Pure data reduction — no side effects, no LLM calls, no I/O beyond the store.
2. **Prompt formatting.** `format_metrics_for_prompt(report, section) -> str` returns a compact text block suitable for injection into `build_diagnosis_prompt` (section=`"findings"`) or `build_interventions_prompt` (section=`"interventions"`). Returns `""` for empty reports so callers can skip the section cleanly.
3. **CLI formatting.** `format_metrics_for_cli(report) -> str` returns a terminal-friendly table.

### Files touched

| File | Change |
|---|---|
| `src/afteragent/effectiveness.py` | NEW — entire module |
| `src/afteragent/llm/prompts.py` | `build_diagnosis_prompt` and `build_interventions_prompt` gain optional `effectiveness_report` parameter; when provided, prepend a `## Historical effectiveness` section before the base context block |
| `src/afteragent/llm/enhancer.py` | Compute `EffectivenessReport` once before the findings call, thread it through both prompt calls; catch aggregation failures and fall back to `effectiveness_report=None` |
| `src/afteragent/cli.py` | New `afteragent stats` subcommand with `--min-samples` flag |
| `tests/test_effectiveness.py` | NEW — unit tests for aggregation + formatters (~18 tests) |
| `tests/test_llm_prompts.py` | Add tests verifying optional parameter behavior (~5 tests) |
| `tests/test_llm_enhancer.py` | Add tests for compute-once-pass-twice behavior and graceful aggregation failure (~2 tests) |
| `tests/test_cli.py` | Add tests for the `stats` subcommand (~3 tests) |
| `scripts/e2e_matrix.sh` | Add `tests/test_effectiveness.py` to the existing LLM diagnosis block |

### Unchanged

`src/afteragent/store.py`, `src/afteragent/workflow.py`, `src/afteragent/diagnostics.py`, `src/afteragent/transcripts.py`, `src/afteragent/adapters.py`, `src/afteragent/capture.py`, `src/afteragent/ui.py`, `src/afteragent/models.py`, `src/afteragent/github.py`, `src/afteragent/config.py`, all other `src/afteragent/llm/*.py` files, `pyproject.toml` (no new dependencies).

## Data model

```python
@dataclass(slots=True, frozen=True)
class EffectivenessMetric:
    """One row of the effectiveness report — either a finding code or an
    intervention (type, target) pair."""
    key: str                  # e.g. "low_diff_overlap" or "prompt_patch/task_prompt"
    kind: str                 # "finding_code" | "intervention_type_target"
    source: str               # "rule" | "llm" | "mixed"
    samples: int              # Number of replays this metric is computed from
    successes: int            # Number of replays where this was a win
    success_rate: float       # successes / samples, between 0.0 and 1.0


@dataclass(slots=True, frozen=True)
class EffectivenessReport:
    """Aggregated effectiveness metrics snapshot."""
    total_replays: int
    min_samples_threshold: int
    finding_metrics: list[EffectivenessMetric]       # Key 1 — sorted by samples desc
    intervention_metrics: list[EffectivenessMetric]  # Key 2 — sorted by samples desc
    generated_at: str                                # ISO-8601 timestamp
```

## Aggregation semantics

### Key 1 — Finding code resolution rate

**Definition:** for each finding code appearing in any source run's stored findings, what fraction of replays have that code in their `comparison_json.resolved_findings` list?

**Measures:** "when this code shows up, does the repair loop actually fix it?" This is the right question for the diagnosis prompt: should the LLM propose this code at all, and if so, is it usually actionable?

**Implementation sketch:**

```python
def _aggregate_finding_metrics(
    replay_rows: list[sqlite3.Row],
    store: Store,
    min_samples: int,
) -> list[EffectivenessMetric]:
    sample_counts: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    source_tags: dict[str, set[str]] = {}

    for row in replay_rows:
        comparison = json.loads(row["comparison_json"])
        resolved_set = set(comparison.get("resolved_findings", []))
        source_findings = store.get_diagnoses(row["source_run_id"])
        for finding_row in source_findings:
            code = finding_row["code"]
            sample_counts[code] = sample_counts.get(code, 0) + 1
            if code in resolved_set:
                success_counts[code] = success_counts.get(code, 0) + 1
            source_tags.setdefault(code, set()).add(finding_row["source"])

    return _build_metrics(
        sample_counts, success_counts, source_tags,
        kind="finding_code",
        min_samples=min_samples,
    )
```

### Key 2 — Intervention (type, target) pair win rate

**Definition:** for each `(type, target)` pair appearing in any source run's intervention manifest, what fraction of replays have `comparison_json.improved == True`?

**Measures:** "when we apply this kind of fix, does the overall run get better?" This is the right question for the interventions prompt: should the LLM author this shape of intervention?

**Critical detail:** each `(type, target)` pair is counted **at most once per replay**, even if the manifest contains multiple interventions with the same pair. Otherwise a manifest with five `prompt_patch/task_prompt` interventions would quintuple-count the replay's outcome.

**Implementation sketch:**

```python
def _aggregate_intervention_metrics(
    replay_rows: list[sqlite3.Row],
    min_samples: int,
) -> list[EffectivenessMetric]:
    sample_counts: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    source_tags: dict[str, set[str]] = {}

    for row in replay_rows:
        comparison = json.loads(row["comparison_json"])
        manifest = json.loads(row["intervention_manifest_json"])
        improved = bool(comparison.get("improved"))
        pairs_seen: set[str] = set()
        for intervention in manifest.get("interventions", []):
            pair = f"{intervention['type']}/{intervention['target']}"
            pairs_seen.add(pair)
            source_tags.setdefault(pair, set()).add(
                intervention.get("source", "rule")
            )
        for pair in pairs_seen:
            sample_counts[pair] = sample_counts.get(pair, 0) + 1
            if improved:
                success_counts[pair] = success_counts.get(pair, 0) + 1

    return _build_metrics(
        sample_counts, success_counts, source_tags,
        kind="intervention_type_target",
        min_samples=min_samples,
    )
```

### `_build_metrics` helper

```python
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

### Sort order

Both metric lists are sorted by `(-samples, -success_rate, key)`:

1. Metrics with more evidence come first. Important because the prompt injection caps at 10 rows — the top 10 are the most confident.
2. Within equal sample counts, higher win rate ranks higher. Stable and informative.
3. Alphabetical key is the final tiebreaker for deterministic test output.

### `compute_effectiveness_metrics` entry point

```python
def compute_effectiveness_metrics(
    store: Store,
    min_samples: int = 5,
) -> EffectivenessReport:
    """Aggregate per-finding and per-(intervention_type, target) win rates
    across all recorded replays. Metrics with fewer than min_samples samples
    are omitted — they'd be statistical noise.
    """
    replay_rows = store.list_all_replay_runs()

    finding_metrics = _aggregate_finding_metrics(replay_rows, store, min_samples)
    intervention_metrics = _aggregate_intervention_metrics(replay_rows, min_samples)

    finding_metrics.sort(key=lambda m: (-m.samples, -m.success_rate, m.key))
    intervention_metrics.sort(key=lambda m: (-m.samples, -m.success_rate, m.key))

    return EffectivenessReport(
        total_replays=len(replay_rows),
        min_samples_threshold=min_samples,
        finding_metrics=finding_metrics,
        intervention_metrics=intervention_metrics,
        generated_at=now_utc(),
    )
```

## Prompt injection

### Formatter

```python
_PROMPT_MAX_ROWS = 10


def format_metrics_for_prompt(
    report: EffectivenessReport,
    section: str,  # "findings" | "interventions"
) -> str:
    """Compact text block for prompt injection. Returns empty string when
    no metrics qualify so callers can skip the section cleanly."""
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
```

### Design rationale

- **`_PROMPT_MAX_ROWS = 10`** caps injection at ~250 tokens even in the worst case. Metrics are sorted by sample count so the top 10 are always the most confident.
- **Returns `""` when no metrics qualify.** Callers skip the section entirely rather than emitting a "(none)" placeholder. Keeps the cold-start prompt clean.
- **No severity threshold on what's shown.** The LLM sees both high-rate and low-rate entries. The goal is "give the LLM evidence," not "hide bad codes."
- **Explicit guidance sentence at the end.** Tells the LLM how to use the data. Without this, the LLM might include the raw numbers in its output `evidence` list, treat them as a hard filter, or ignore them.

### Integration with `build_diagnosis_prompt`

Both prompt builders gain an optional `effectiveness_report: EffectivenessReport | None = None` kwarg. When present, the effectiveness block is prepended BEFORE the base context block. Rationale: the LLM reads top-down, and historical data is framing context that should shape how it interprets the run-specific data below.

```python
def build_diagnosis_prompt(
    context: DiagnosisContext,
    effectiveness_report: EffectivenessReport | None = None,
) -> tuple[str, str]:
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

Same pattern for `build_interventions_prompt` with `section="interventions"`.

**Backward compatibility:** all existing callers that don't pass the new kwarg see identical behavior. Only `enhance_diagnosis_with_llm` will be updated to pass the report.

**Token budget impact:** injection is ~250 tokens max. `_enforce_token_budget` already trims transcript events first when oversized, so the new block is safe.

## Enhancer integration

```python
from ..effectiveness import compute_effectiveness_metrics


def enhance_diagnosis_with_llm(
    store: Store,
    run_id: str,
    client: LLMClient,
    config: LLMConfig,
) -> EnhanceResult:
    # ... (existing setup — context loading, rule-based findings)

    context = load_diagnosis_context(store, run_id)

    # Compute the effectiveness report once. If it fails, fall back cleanly.
    try:
        effectiveness_report = compute_effectiveness_metrics(store)
    except Exception:
        effectiveness_report = None

    # ----- Findings call -----
    system, user = build_diagnosis_prompt(
        context,
        effectiveness_report=effectiveness_report,
    )
    # ... (existing LLM call + merge)

    # ----- Interventions call -----
    system, user = build_interventions_prompt(
        context,
        merged,
        effectiveness_report=effectiveness_report,
    )
    # ... (existing LLM call + persist)
```

**One computation, two calls.** The report is computed once and threaded through both prompt builders. No caching — each enhance call reads fresh data from the store.

**Failure isolation.** If `compute_effectiveness_metrics` raises, the enhancer catches it and passes `None` to both prompt calls. The run still enhances, just without the historical context. The existing sub-project 2 behavior is the fallback.

## CLI integration

### Subcommand parser

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

### Dispatch

```python
if args.command == "stats":
    from .effectiveness import compute_effectiveness_metrics, format_metrics_for_cli

    report = compute_effectiveness_metrics(store, min_samples=args.min_samples)
    print(format_metrics_for_cli(report))
    return 0
```

### CLI formatter

```python
def format_metrics_for_cli(report: EffectivenessReport) -> str:
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

### Example output

```
AfterAgent effectiveness (27 total replays, min_samples=5)

Finding code resolution rates:
  low_diff_overlap_with_failing_files               78% (21/27, source=mixed)
  comments_ignored_after_they_existed               56% (9/16, source=rule)
  broad_edit_drift                                  41% (7/17, source=rule)
  novel_agent_read_edit_loop                        60% (3/5, source=llm)

Intervention (type/target) win rates:
  prompt_patch/task_prompt                          72% (18/25, source=mixed)
  instruction_patch/repo_instructions               64% (14/22, source=mixed)
  runtime_guardrail/runner_policy                   33% (4/12, source=rule)
```

## Error handling

Nothing in sub-project 3 is allowed to fail the enhance call. The enhancer's contract from sub-project 2 is "never raise, always preserve rule-based state." Sub-project 3 preserves that by wrapping the new computation in the enhancer's existing safety net.

| Failure | Response |
|---|---|
| `compute_effectiveness_metrics` raises (corrupt store, malformed row the aggregator couldn't recover from) | Enhancer catches via try/except and passes `effectiveness_report=None` to both prompt calls. Run enhances normally without the historical context. |
| Single replay row has malformed `comparison_json` or `intervention_manifest_json` | Aggregator's per-row try/except skips the row and continues producing samples for that row. The row is not logged per-row (noisy); `total_replays` still counts every replay row even when `comparison_json` or `intervention_manifest_json` are malformed, but per-metric sample counts shrink because the per-row try/except skips producing samples from the malformed row. Only successfully parsed rows contribute samples. Note: if `store.get_diagnoses(source_run_id)` returns empty (source run deleted), the Aggregator processes the intervention metrics but contributes zero finding samples from that row. |
| `store.get_diagnoses(source_run_id)` returns empty for a historical replay's source (source run was deleted) | Aggregator processes the replay row but contributes zero findings from it. Intervention metrics still count if the manifest is intact. |
| Empty store (no replays) | `compute_effectiveness_metrics` returns a report with `total_replays=0` and empty metric lists. `format_metrics_for_prompt` returns `""`. `format_metrics_for_cli` prints "No replays recorded yet." All clean no-ops. |
| All metrics below `min_samples` threshold | `total_replays > 0` but both metric lists are empty. Prompt injection is empty-string. CLI prints a header with "(no codes with ≥N samples)". |
| Source findings overwritten since replay time | Aggregator uses the current findings as ground truth. **Documented as a known imprecision**; correct fix is a schema change (snapshot findings into the manifest at replay time), deferred. |

### Contracts

- **`compute_effectiveness_metrics`** never raises on a valid store. Per-row failures are swallowed inside the aggregator. The wrapping try/except in the enhancer is defense-in-depth.
- **`format_metrics_for_prompt`** never raises on a valid `EffectivenessReport`. Returns `""` for empty reports. Raises `ValueError` only if the `section` argument is unknown (caller bug).
- **`format_metrics_for_cli`** never raises on a valid report. Handles all branches (empty store / below threshold / populated) explicitly.

## Testing strategy

### Unit tests — `tests/test_effectiveness.py` (~18 tests)

```
test_compute_effectiveness_metrics_empty_store
test_compute_effectiveness_metrics_returns_finding_code_resolution_rates
test_compute_effectiveness_metrics_respects_min_samples_threshold
test_compute_effectiveness_metrics_deduplicates_intervention_pair_per_replay
test_compute_effectiveness_metrics_aggregates_intervention_win_rate_from_improved_flag
test_compute_effectiveness_metrics_source_tagging
test_compute_effectiveness_metrics_sorts_by_samples_then_rate_then_key
test_compute_effectiveness_metrics_skips_corrupt_replay_rows
test_compute_effectiveness_metrics_handles_deleted_source_run

test_format_metrics_for_prompt_empty_report_returns_empty_string
test_format_metrics_for_prompt_findings_section_shape
test_format_metrics_for_prompt_interventions_section_shape
test_format_metrics_for_prompt_caps_at_max_rows
test_format_metrics_for_prompt_rejects_unknown_section_name

test_format_metrics_for_cli_empty_store
test_format_metrics_for_cli_below_threshold
test_format_metrics_for_cli_populated
```

### Integration tests — prompt builders (`tests/test_llm_prompts.py`, ~5 tests)

```
test_build_diagnosis_prompt_includes_effectiveness_section_when_report_passed
test_build_diagnosis_prompt_omits_effectiveness_section_when_report_none
test_build_interventions_prompt_includes_effectiveness_section_when_report_passed
test_build_interventions_prompt_omits_effectiveness_section_when_report_none
test_build_diagnosis_prompt_still_respects_token_budget_with_effectiveness_block
```

### Integration tests — enhancer (`tests/test_llm_enhancer.py`, ~2 tests)

```
test_enhance_computes_effectiveness_report_once_and_passes_to_both_calls
test_enhance_tolerates_effectiveness_computation_failure
```

### Integration tests — CLI (`tests/test_cli.py`, ~3 tests)

```
test_stats_subcommand_empty_store
test_stats_subcommand_populated_store
test_stats_subcommand_accepts_min_samples_flag
```

### E2E matrix

`scripts/e2e_matrix.sh` gets `tests/test_effectiveness.py` appended to the existing LLM diagnosis block — effectiveness tests run alongside sub-project 2's tests since they share the same integration surface.

## Success criteria

Sub-project 3 ships when **all** of the following are true:

1. `afteragent stats` on an empty store prints "No replays recorded yet." and exits 0.
2. `afteragent stats` on a populated store prints a table with finding-code resolution rates and intervention-type win rates, both filtered by `min_samples` (default 5).
3. `afteragent enhance <run-id>` with replay data present injects a "## Historical effectiveness" section into both the diagnosis and interventions prompts.
4. When `compute_effectiveness_metrics` raises (simulated), the enhancer still completes successfully and the prompts fall back to the sub-project 2 shape without the effectiveness block.
5. `afteragent enhance` on a store with zero replays works identically to sub-project 2 — no effectiveness block in the prompt, no visible difference from the user's perspective.
6. Rule-based diagnosis paths (`afteragent exec` without `--enhance`, `afteragent diagnose`) are completely unchanged. Sub-project 3 is invisible to users who don't use the LLM layer.
7. All tests pass: sub-project 2's 179 tests still green + 30 new tests across effectiveness, prompts, enhancer, and CLI ≈ ≈209 total pytest tests.
8. **Manual dogfood acceptance:** accumulate ≥5 replays on a real project, run `afteragent stats`, confirm the numbers match the stored `comparison_json` rows by spot-check. Then run `afteragent enhance <run-id>` with real LLM credentials and confirm the effectiveness block appears in the prompt (via enhanced logging or by inspecting the LLM's response reasoning). This is the quality-bar step that requires real replay data + real LLM credentials.

## Known followups (non-blocking)

- **Key 3 — finding code × intervention type pair.** Richer signal, needs more data. Add when Keys 1 + 2 prove insufficient.
- **Snapshot findings into replay manifests.** Fixes the "source findings overwritten" imprecision. Schema migration + backfill.
- **Hard-filter guardrail (option C).** Strip findings or interventions with catastrophic win rates before persisting. Add once enough data exists to calibrate thresholds.
- **Time decay / recency weighting.** Weight recent replays higher than old ones.
- **Per-repo effectiveness scoping.** Slice metrics by repo or PR for users running AfterAgent across multiple projects.
- **UI extension.** Sub-project 5 will surface this in the browser alongside the narrative report.
- **`afteragent cost` or `afteragent llm-history` commands** for inspecting `llm_generations` rows. Orthogonal to effectiveness but semantically adjacent.