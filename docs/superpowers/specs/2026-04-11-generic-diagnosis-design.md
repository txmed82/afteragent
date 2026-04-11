# Sub-Project 4: Broaden Past PR Repair — Design

**Status:** Design approved, pending spec review
**Date:** 2026-04-11
**Owner:** Colin
**Scope:** Sub-project 4 of 5 in the AfterAgent self-improvement arc.
**Depends on:** Sub-projects 1–3, all shipped in v0.3.0.

---

## Context

AfterAgent's 6 rule-based detectors in `src/afteragent/diagnostics.py` are all PR-centric. They read from `github_context.json` — pulled via `gh` CLI — and fire on signals like unresolved review threads, failing CI checks, and diff-vs-failing-files overlap. When a user runs `afteragent exec -- claude "build a feature"` without a PR, `github_context.json` is empty, the 6 detectors all silently return no findings, and the user gets a capture with essentially zero diagnosis.

That's the ~80% of agent work AfterAgent currently underserves: feature development, refactors, research spikes, local debugging, deploys, one-off scripts. None of those involve a PR. All of them can still go wrong in ways worth detecting.

Sub-project 4 adds a parallel rule-based track of generic detectors that work from transcript events + diff + stdout/stderr + a newly-captured task prompt — no GitHub context required. It keeps the "rule-based floor, LLM ceiling" philosophy sub-projects 1–3 established: users without LLM providers still get useful output, users with providers get richer output because the LLM also receives the task prompt and transcript signal.

Sub-project 4 does NOT change `compare_runs`, replay scoring, or the existing 6 PR detectors. Scoring generic-run replays is a genuinely hard problem (what does "improved" mean without review threads or CI checks?) and deserves its own design — deferred to a future sub-project 4.5.

Sub-project 5 (narrative UI) will consume the new task_prompt column and the new generic findings when it surfaces runs in the browser.

## Goals

1. Capture the user's task prompt at exec time via a three-tier fallback (explicit `--task` CLI flag → adapter-parsed from the command → full `shlex.join(command)`) and persist it to a new `runs.task_prompt` column.
2. Add a new `RunnerAdapter.parse_task_prompt(command) -> str | None` method with overrides on `ClaudeCodeAdapter` and `CodexAdapter` for runner-specific parsing.
3. Add 5 new rule-based detectors in `src/afteragent/diagnostics_generic.py` that work from transcript events + diff + task prompt alone:
   - `agent_edits_without_tests`
   - `agent_stuck_on_file`
   - `agent_read_edit_divergence`
   - `agent_command_failure_hidden`
   - `agent_zero_meaningful_activity`
4. Extend `analyze_run` to call the new detector track alongside the existing 6 PR detectors. Both tracks always run; PR detectors self-skip on empty `gh_context`; generic detectors self-skip on empty transcript events / diff.
5. Extend `_build_base_context_block` in the LLM prompt builder to include the task prompt as a new section at the top of the user message, regardless of PR-ness. The LLM now sees the user's intent on every enhance call.
6. Extend the `runs` table schema with an additive `task_prompt` column. No backfill — historical runs get `NULL`.
7. Preserve the never-break-the-run contract: every failure mode in sub-project 4 degrades to "run as if sub-project 4 didn't exist."

## Non-goals

- **No changes to `compare_runs`, `replay_run`, or replay scoring.** Generic-mode replay scoring is deferred to sub-project 4.5.
- **No new intervention TYPES.** The existing 5 intervention types from sub-project 2 cover the new finding codes. `build_interventions` gains new `if` branches for the new codes — boilerplate, not new vocabulary.
- **No auto-task-classification.** The task prompt is stored and surfaced as raw text. No LLM-based categorization into refactor/feature/bug-fix/etc.
- **No multi-turn task tracking.** Each run is independent; no grouping by shared task.
- **No UI changes.** Sub-project 5's narrative UI owns the browser surface.
- **No changes to `github.py` or `capture_github_context`.** Generic runs already produce empty github_context.json; no refactoring needed.
- **No tuning of detector thresholds via config.** `_STUCK_FILE_EDIT_THRESHOLD = 4` and `_MIN_MEANINGFUL_EVENTS = 3` are source constants.
- **No LLM-based generic detectors.** Sub-project 2's `enhance_diagnosis_with_llm` already handles non-PR runs; this sub-project just gives it the task prompt and transcript signal.
- **No backfill of `task_prompt` on existing runs.** Pre-existing runs get `NULL`.
- **No task prompt search / filtering in `afteragent runs`.** CLI follow-up, not in scope.
- **No restructure of `diagnostics.py` into a sub-package.** Existing code stays untouched; new detectors live in a sibling file.
- **No threshold overrides for individual detectors via CLI flags.** Source constants only.

## Architecture

### New files

```
src/afteragent/diagnostics_generic.py    # 5 generic detectors + run_generic_detectors entry
tests/test_diagnostics_generic.py         # ~12 unit tests for the detectors
```

### Modified files

| File | Change |
|---|---|
| `src/afteragent/diagnostics.py` | `analyze_run` gains one `findings.extend(run_generic_detectors(context, store))` call after the existing 6 detectors. `load_run_context` adds `transcript_events` to the returned dict. `build_interventions` gains 5 new `if` branches for the new finding codes. |
| `src/afteragent/adapters.py` | `RunnerAdapter` base class gains `parse_task_prompt(command) -> str | None` returning `None` by default. `ClaudeCodeAdapter` and `CodexAdapter` override with runner-specific parsing. `ShellAdapter` and `OpenClawAdapter` inherit the base `None`. |
| `src/afteragent/capture.py` | `run_command` gains `task_prompt: str | None = None` parameter. Resolves via three-tier fallback. Calls `store.set_run_task_prompt(run_id, resolved)` right after `create_run`. |
| `src/afteragent/store.py` | Additive migration via `_ensure_column` for `runs.task_prompt`. New method `set_run_task_prompt(run_id, task_prompt)`. Existing `get_run` / `list_runs` / `list_previous_runs` return the new column via `RunRecord`. |
| `src/afteragent/models.py` | `RunRecord` dataclass gains a `task_prompt: str | None` field. |
| `src/afteragent/cli.py` | `exec` subparser gains `--task` flag. `exec` dispatch passes it through to `run_command(..., task_prompt=args.task_prompt)`. |
| `src/afteragent/llm/prompts.py` | `_build_base_context_block` prepends a `## Task prompt` section when `context.run.task_prompt` is non-empty. |
| `tests/test_adapters.py` | ~8 new tests for adapter `parse_task_prompt` overrides. |
| `tests/test_capture.py` | ~3 integration tests for the resolution chain. |
| `tests/test_diagnostics.py` | ~2 tests for `analyze_run` orchestration with generic detectors. |
| `tests/test_llm_prompts.py` | ~2 tests for the new task prompt section. |
| `tests/test_cli.py` | ~2 tests for the `--task` flag. |
| `tests/test_store_task_prompt.py` | NEW — ~4 tests for the column migration and round-trip. |
| `scripts/e2e_matrix.sh` | Add `test_diagnostics_generic.py` and `test_store_task_prompt.py` to existing blocks. |

### Unchanged

`workflow.py`, `ui.py`, `github.py`, `transcripts.py`, `effectiveness.py`, all `llm/*.py` files except `prompts.py`, `pyproject.toml`, `README.md`.

## Task prompt capture

### Database migration

Additive column on the existing `runs` table via `_ensure_column`:

```python
# Inside Store._init_db, alongside the existing _ensure_column calls:
self._ensure_column(conn, "runs", "task_prompt", "TEXT")
```

Nullable. Existing rows default to `NULL`. No backfill.

### `RunRecord` update

```python
@dataclass(slots=True)
class RunRecord:
    id: str
    command: str
    cwd: str
    status: str
    exit_code: int | None
    created_at: str
    finished_at: str | None
    duration_ms: int | None
    summary: str | None
    task_prompt: str | None  # NEW — last field, nullable
```

### Store update

New method `set_run_task_prompt(run_id, task_prompt)` — single UPDATE. Called from `capture.run_command` after `create_run` so the existing `create_run` signature stays stable. Existing `get_run` / `list_runs` / `list_previous_runs` construct `RunRecord(**dict(row))` — with the new column in the schema and the new dataclass field, round-trip works unchanged.

### Adapter method contract

```python
class RunnerAdapter:
    # ... existing methods ...

    def parse_task_prompt(self, command: list[str]) -> str | None:
        """Extract the user-facing task prompt from the runner invocation.

        Default implementation returns None — callers fall back to
        shlex.join(command) as the last-resort task prompt.
        """
        del command
        return None
```

### `ClaudeCodeAdapter.parse_task_prompt`

```python
def parse_task_prompt(self, command: list[str]) -> str | None:
    if len(command) < 2:
        return None
    # Supported shapes:
    #   claude "fix the failing test"
    #   claude -p "fix the failing test"
    #   claude --print "fix the failing test"
    #   claude --dangerously-skip-permissions -p "fix the failing test"
    #   claude --dangerously-skip-permissions "fix the failing test"
    #   claude --print="quick task"
    args = command[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-p", "--print"):
            if i + 1 < len(args):
                return args[i + 1]
            return None
        if arg.startswith("--print="):
            return arg[len("--print="):]
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return None
```

### `CodexAdapter.parse_task_prompt`

```python
def parse_task_prompt(self, command: list[str]) -> str | None:
    if len(command) < 2:
        return None
    # Supported shapes:
    #   codex "fix the failing test"
    #   codex run "fix the failing test"
    #   codex -p "fix the failing test"
    #   codex --prompt "summarize changes"
    args = command[1:]
    if args and args[0] == "run":
        args = args[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-p", "--prompt"):
            if i + 1 < len(args):
                return args[i + 1]
            return None
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return None
```

### `OpenClawAdapter` and `ShellAdapter`

Inherit the base `None` default. Shell-style invocations (`python3 run_agent.py`, `bash repair.sh`) don't have a reliable task-prompt convention — the caller falls back to `shlex.join(command)`.

### `capture.run_command` resolution chain

```python
def run_command(
    store: Store,
    command: list[str],
    cwd: Path,
    summary: str | None = None,
    github_repo: str | None = None,
    github_pr: int | None = None,
    stream_output: bool = True,
    extra_env: dict[str, str] | None = None,
    adapter: RunnerAdapter | None = None,
    task_prompt: str | None = None,  # NEW
) -> dict[str, str | int]:
    active_adapter = adapter or select_runner_adapter(cwd, command=command)

    # Three-tier resolution: explicit param > adapter parse > full command.
    if task_prompt is not None:
        resolved_task_prompt = task_prompt
    else:
        parsed = active_adapter.parse_task_prompt(command)
        resolved_task_prompt = parsed if parsed is not None else shlex.join(command)

    run_id = uuid.uuid4().hex[:12]
    # ... (existing store.create_run call unchanged) ...
    store.set_run_task_prompt(run_id, resolved_task_prompt)
    # ... (rest of run_command unchanged) ...
```

### CLI flag

```python
exec_parser.add_argument(
    "--task",
    dest="task_prompt",
    help="Override the auto-detected task prompt with an explicit string",
)
```

Passed through in the `exec` dispatch:

```python
result = run_command(
    store,
    command,
    Path.cwd(),
    summary=args.summary,
    stream_output=not args.no_stream,
    task_prompt=getattr(args, "task_prompt", None),
)
```

### Example extractions

| Command | Extracted `task_prompt` |
|---|---|
| `claude "fix the failing test"` | `fix the failing test` |
| `claude -p "build dark mode"` | `build dark mode` |
| `claude --dangerously-skip-permissions -p "refactor auth"` | `refactor auth` |
| `claude --print="quick task"` | `quick task` |
| `codex run "add logging"` | `add logging` |
| `codex --prompt "summarize changes"` | `summarize changes` |
| `python3 scripts/run_agent.py` | `python3 scripts/run_agent.py` (full command fallback) |
| `afteragent exec --task "deploy to staging" -- bash deploy.sh` | `deploy to staging` (explicit override) |

### Fallback correctness

When extraction falls back to `shlex.join(command)`, downstream consumers still work:
- `agent_read_edit_divergence` uses the task prompt for substring matching — the full command produces fewer matches than a clean prompt, so the detector fires less confidently on shell-script agents. Acceptable.
- The LLM prompt's task section shows the full command instead of a clean prompt. LLMs handle this fine — they infer intent from other signals.
- `agent_edits_without_tests` doesn't use the task prompt at all. Unaffected.

## Generic detectors

### Entry point

```python
# src/afteragent/diagnostics_generic.py
from __future__ import annotations

import re

from .models import PatternFinding
from .store import Store


def run_generic_detectors(
    context: dict,
    store: Store,
) -> list[PatternFinding]:
    """Run all 5 generic (non-PR) detectors against a run's context.

    Each detector is a pure function returning 0 or 1 PatternFinding.
    Empty results when the detector has no signal.
    """
    findings: list[PatternFinding] = []
    for detector in _DETECTORS:
        try:
            result = detector(context, store)
        except Exception:
            continue  # Individual detector failures never break analyze_run
        if result is not None:
            findings.append(result)
    return findings


_DETECTORS: list = []  # Populated below with the 5 detector functions
```

Per-detector `try/except` is defensive — a buggy detector can't crash the pipeline. Matches sub-project 1's contract.

### Detector 1: `agent_edits_without_tests`

**Signal:** the agent made at least one file edit but never ran a test command.

```python
_TEST_COMMAND_PATTERNS = (
    re.compile(r"\bpytest\b"),
    re.compile(r"\bpython\s+-m\s+pytest\b"),
    re.compile(r"\bjest\b"),
    re.compile(r"\bnpm\s+(?:run\s+)?test\b"),
    re.compile(r"\byarn\s+test\b"),
    re.compile(r"\bgo\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\bmocha\b"),
    re.compile(r"\bvitest\b"),
    re.compile(r"\brspec\b"),
    re.compile(r"\bbundle\s+exec\s+rspec\b"),
    re.compile(r"\btox\b"),
    re.compile(r"\bunittest\b"),
)


def _detect_edits_without_tests(context: dict, store: Store) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    diff_has_edits = bool(context.get("changed_files"))
    if not diff_has_edits and not _any_event_kind(transcript_events, "file_edit"):
        return None

    for event in transcript_events:
        if event.kind == "test_run":
            return None
        if event.kind == "bash_command":
            target = event.target or ""
            if any(p.search(target) for p in _TEST_COMMAND_PATTERNS):
                return None

    changed_files = sorted(context.get("changed_files") or set())
    return PatternFinding(
        code="agent_edits_without_tests",
        title="Agent edited files but never ran tests",
        severity="medium",
        summary=(
            "The agent modified one or more files during this run but did not "
            "execute any test command. The change landed unverified — the next "
            "run should confirm the edits behave as intended."
        ),
        evidence=[
            f"Changed files: {', '.join(changed_files[:5]) or 'none in diff but edit events present'}",
            f"Transcript events: {len(transcript_events)}",
        ],
    )


_DETECTORS.append(_detect_edits_without_tests)
```

### Detector 2: `agent_stuck_on_file`

**Signal:** 4+ consecutive edit events targeting the same file without any test run in between.

```python
_STUCK_FILE_EDIT_THRESHOLD = 4


def _detect_stuck_on_file(context: dict, store: Store) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    if not transcript_events:
        return None

    edits_by_file: dict[str, int] = {}
    max_streak: dict[str, int] = {}

    for event in transcript_events:
        if event.kind == "test_run":
            edits_by_file.clear()
            continue
        if event.kind == "file_edit" and event.target:
            path = event.target
            edits_by_file[path] = edits_by_file.get(path, 0) + 1
            if edits_by_file[path] > max_streak.get(path, 0):
                max_streak[path] = edits_by_file[path]

    stuck_files = {
        path: count
        for path, count in max_streak.items()
        if count >= _STUCK_FILE_EDIT_THRESHOLD
    }
    if not stuck_files:
        return None

    path, count = max(stuck_files.items(), key=lambda pair: pair[1])
    all_stuck = sorted(stuck_files.items(), key=lambda pair: -pair[1])
    return PatternFinding(
        code="agent_stuck_on_file",
        title=f"Agent edited {path} {count} times without running tests",
        severity="high",
        summary=(
            f"The agent made {count} consecutive edits to {path} without a test "
            f"run between them. This is usually a sign of a stuck edit loop — "
            f"the agent is making guesses without measuring the outcome."
        ),
        evidence=[
            f"{path}: {count} consecutive edits"
            for path, count in all_stuck[:5]
        ],
    )


_DETECTORS.append(_detect_stuck_on_file)
```

### Detector 3: `agent_read_edit_divergence`

**Signal:** agent read some files and edited different files with zero overlap between the two sets.

```python
def _detect_read_edit_divergence(context: dict, store: Store) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    if not transcript_events:
        return None

    read_paths: set[str] = set()
    edit_paths: set[str] = set()
    for event in transcript_events:
        if event.kind == "file_read" and event.target:
            read_paths.add(event.target)
        elif event.kind == "file_edit" and event.target:
            edit_paths.add(event.target)

    if len(read_paths) < 2 or len(edit_paths) < 1:
        return None

    overlap = read_paths & edit_paths
    if overlap:
        return None

    return PatternFinding(
        code="agent_read_edit_divergence",
        title="Agent read files it never edited and edited files it never read",
        severity="medium",
        summary=(
            f"The agent read {len(read_paths)} files and edited {len(edit_paths)} "
            f"files, but there's zero overlap between the two sets. Typically "
            f"this means the agent got oriented on one part of the codebase and "
            f"then made changes somewhere unrelated."
        ),
        evidence=[
            f"Read (not edited): {', '.join(sorted(read_paths)[:5])}",
            f"Edited (not read): {', '.join(sorted(edit_paths)[:5])}",
        ],
    )


_DETECTORS.append(_detect_read_edit_divergence)
```

### Detector 4: `agent_command_failure_hidden`

**Signal:** process exited non-zero but the final assistant message sounds like a success claim.

```python
_SUCCESS_CLAIM_PATTERNS = (
    re.compile(r"\b(fixed|done|complete|completed|ready|success(?:ful)?)\b", re.I),
    re.compile(r"\ball\s+tests\s+pass(?:ing)?\b", re.I),
    re.compile(r"\bfinished\b", re.I),
    re.compile(r"\bshould\s+(?:work|be)\s+(?:now|ready|good)\b", re.I),
)


def _detect_command_failure_hidden(context: dict, store: Store) -> PatternFinding | None:
    run = context.get("run")
    if run is None or run.exit_code in (None, 0):
        return None

    transcript_events = context.get("transcript_events") or []
    final_assistant_message: str | None = None
    for event in reversed(transcript_events):
        if event.kind == "assistant_message" and event.output_excerpt:
            final_assistant_message = event.output_excerpt
            break

    if final_assistant_message is None:
        return None

    if not any(p.search(final_assistant_message) for p in _SUCCESS_CLAIM_PATTERNS):
        return None

    return PatternFinding(
        code="agent_command_failure_hidden",
        title=f"Agent claimed success but process exited with code {run.exit_code}",
        severity="high",
        summary=(
            f"The run exited with code {run.exit_code}, but the agent's final "
            f"assistant message sounds like a success claim. The next run should "
            f"explicitly verify the task is actually done — the current state is "
            f"lying about its own outcome."
        ),
        evidence=[
            f"Exit code: {run.exit_code}",
            f"Final assistant message: {final_assistant_message[:200]}",
        ],
    )


_DETECTORS.append(_detect_command_failure_hidden)
```

### Detector 5: `agent_zero_meaningful_activity`

**Signal:** very few transcript events AND no diff.

```python
_MIN_MEANINGFUL_EVENTS = 3


def _detect_zero_meaningful_activity(context: dict, store: Store) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    changed_files = context.get("changed_files") or set()

    meaningful_kinds = {
        "file_read",
        "file_edit",
        "bash_command",
        "test_run",
        "search",
        "web_fetch",
        "todo_update",
        "subagent_call",
    }
    meaningful_count = sum(
        1 for e in transcript_events if e.kind in meaningful_kinds
    )

    if meaningful_count >= _MIN_MEANINGFUL_EVENTS:
        return None
    if len(changed_files) >= 1:
        return None

    run = context.get("run")
    return PatternFinding(
        code="agent_zero_meaningful_activity",
        title="Agent produced minimal activity and no code changes",
        severity="medium",
        summary=(
            f"The agent performed only {meaningful_count} meaningful tool "
            f"invocation(s) and the diff is empty. The run either hit an early "
            f"error, the agent was confused about the task, or the task was "
            f"completed without needing changes. The next run should clarify "
            f"the task intent if it remains unresolved."
        ),
        evidence=[
            f"Meaningful transcript events: {meaningful_count}",
            f"Changed files: {len(changed_files)}",
            f"Task prompt: {(run.task_prompt or '')[:150] if run else 'unknown'}",
        ],
    )


_DETECTORS.append(_detect_zero_meaningful_activity)
```

### Shared helper

```python
def _any_event_kind(events: list, kind: str) -> bool:
    return any(e.kind == kind for e in events)
```

## Orchestration integration

In `src/afteragent/diagnostics.py`, `analyze_run` gains two new lines:

```python
def analyze_run(store: Store, run_id: str) -> tuple[list[PatternFinding], list[Intervention]]:
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")

    context = load_run_context(store, run_id)
    related_contexts = load_related_contexts(store, run.id, run.cwd, run.created_at, context["gh_context"])

    findings: list[PatternFinding] = []

    # ... (existing 6 detector blocks unchanged) ...

    # NEW: run the generic (non-PR) detectors on the same context.
    from .diagnostics_generic import run_generic_detectors
    findings.extend(run_generic_detectors(context, store))

    interventions = build_interventions(findings)
    # ... (existing persist logic unchanged) ...
```

Lazy import of `diagnostics_generic` inside the function avoids any circular import risk and makes the single-call-site consumption explicit.

`load_run_context` adds `transcript_events` to the returned context:

```python
def load_run_context(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    # ... (existing setup) ...
    transcript_events = store.get_transcript_events(run_id)  # NEW — from sub-project 1
    return {
        "run": run,  # now has .task_prompt via the RunRecord update
        # ... (existing keys unchanged) ...
        "transcript_events": transcript_events,  # NEW
    }
```

No breaking change to the 6 existing PR detectors — they don't read `transcript_events`, so the new key is ignored by them.

## Build_interventions updates

`build_interventions` in `diagnostics.py` gets 5 new `if` branches, one per new finding code. The hardcoded intervention text for each follows the sub-project 2 pattern (second-person imperative, named files where possible). Example for `agent_edits_without_tests`:

```python
if "agent_edits_without_tests" in codes:
    interventions.append(
        Intervention(
            type="instruction_patch",
            title="Require a test run after edits",
            target="repo_instructions",
            content=(
                "Before declaring a task complete, run the project's test command "
                "(pytest, npm test, go test, etc.) and summarize the result. Never "
                "finish an edit cycle without verifying it."
            ),
            scope="pr",
        )
    )
    interventions.append(
        Intervention(
            type="prompt_patch",
            title="Test after every edit",
            target="task_prompt",
            content=(
                "After each file edit in this run, run the relevant test command "
                "and quote the outcome in your next message before moving to the "
                "next edit."
            ),
            scope="pr",
        )
    )
```

Similar interventions (instruction_patch + prompt_patch pairs) for the other 4 new finding codes. This is boilerplate expansion of the existing `build_interventions` structure, not new vocabulary.

## LLM prompt integration

`_build_base_context_block` in `llm/prompts.py` prepends a `## Task prompt` section at the top when `context.run.task_prompt` is non-empty:

```python
def _build_base_context_block(
    context: DiagnosisContext,
    include_findings_header: str | None,
) -> str:
    sections: list[str] = []

    # NEW: task prompt section at the top when available.
    if context.run.task_prompt:
        sections.append(
            f"## Task prompt\n\n{context.run.task_prompt}"
        )

    # ... (existing run metadata, rule-based findings, transcript events,
    #      diff, stdout/stderr, github summary — unchanged) ...

    return "\n\n".join(sections)
```

Works for both PR and generic runs. Adds ~20–100 tokens to the prompt, well within the 25k budget.

## Error handling

The never-break-the-run contract applies. Every sub-project 4 failure mode degrades to "run as if sub-project 4 didn't exist."

| Failure | Response |
|---|---|
| `parse_task_prompt` raises on a malformed command list | `run_command` catches, falls back to `shlex.join(command)`, run continues. |
| `store.set_run_task_prompt` fails (DB locked) | Propagate — genuine failure, same guarantee as existing `create_run`. |
| `_ensure_column` fails on `task_prompt` | Same as any existing `_ensure_column` failure — idempotent, retried on next `Store` construction. |
| A single generic detector raises | Per-detector try/except catches it, skips, continues. |
| All 5 generic detectors raise | `run_generic_detectors` returns empty list; `analyze_run` proceeds with PR findings only. |
| `context["transcript_events"]` missing or None | Detectors treat it as empty list via `context.get("transcript_events") or []`. |
| `run.task_prompt` is None (legacy row) | Detectors using it (only `agent_zero_meaningful_activity`) show `"unknown"` in evidence. Not a failure. |
| Task prompt is the full command string fallback | Detectors work with reduced confidence; no crash. |
| `agent_command_failure_hidden` reads empty transcript | Returns None via the guard. |

## Testing strategy

### Unit tests — `tests/test_diagnostics_generic.py` (~12 tests)

```
test_run_generic_detectors_empty_context_returns_empty_list
test_run_generic_detectors_isolates_detector_failures

test_edits_without_tests_fires_when_diff_has_edits_and_no_test_run
test_edits_without_tests_skipped_when_pytest_was_run
test_edits_without_tests_skipped_when_diff_is_empty_and_no_edit_events

test_stuck_on_file_fires_at_threshold_of_four_edits
test_stuck_on_file_resets_counter_on_test_run
test_stuck_on_file_picks_highest_streak_file

test_read_edit_divergence_fires_when_zero_overlap
test_read_edit_divergence_skipped_when_files_overlap
test_read_edit_divergence_skipped_below_activity_threshold

test_command_failure_hidden_fires_on_nonzero_exit_with_success_claim
test_command_failure_hidden_skipped_on_zero_exit
test_command_failure_hidden_skipped_when_no_success_claim

test_zero_meaningful_activity_fires_on_minimal_events_and_empty_diff
test_zero_meaningful_activity_skipped_when_activity_threshold_met
test_zero_meaningful_activity_skipped_when_diff_has_changes
```

### Adapter tests — `tests/test_adapters.py` additions (~8 tests)

```
test_claude_adapter_parses_trailing_prompt
test_claude_adapter_parses_dash_p_flag
test_claude_adapter_parses_long_print_flag
test_claude_adapter_parses_equals_print_flag
test_claude_adapter_parses_prompt_after_permission_flag
test_codex_adapter_parses_run_subcommand_prompt
test_codex_adapter_parses_prompt_flag
test_codex_adapter_returns_none_for_command_only
```

### Store tests — `tests/test_store_task_prompt.py` (new file, ~4 tests)

```
test_run_record_roundtrips_task_prompt
test_set_run_task_prompt_updates_existing_row
test_set_run_task_prompt_migration_idempotent_on_existing_runs_table
test_existing_runs_get_null_task_prompt_before_being_rewritten
```

### Integration tests — `tests/test_capture.py` additions (~3 tests)

```
test_run_command_populates_task_prompt_from_adapter_parse
test_run_command_explicit_task_kwarg_wins_over_adapter_parse
test_run_command_falls_back_to_full_command_when_adapter_returns_none
```

### Integration tests — `tests/test_diagnostics.py` additions (~2 tests)

```
test_analyze_run_runs_generic_detectors_alongside_pr_detectors
test_analyze_run_generic_detectors_isolated_from_pr_detector_failures
```

### Integration tests — `tests/test_llm_prompts.py` additions (~2 tests)

```
test_build_diagnosis_prompt_includes_task_prompt_section_when_set
test_build_diagnosis_prompt_omits_task_prompt_section_when_null
```

### CLI tests — `tests/test_cli.py` additions (~2 tests)

```
test_exec_accepts_task_flag
test_exec_populates_task_prompt_from_claude_command_auto
```

Total: ~33 new tests. With 209 from v0.3.0, the full suite lands around ~242.

### E2E matrix

`scripts/e2e_matrix.sh` gets the new test files added to existing blocks:
- `tests/test_diagnostics_generic.py` → LLM diagnosis block (same integration surface as sub-project 3)
- `tests/test_store_task_prompt.py` → transcript ingestion block (store tests)

## Success criteria

Sub-project 4 ships when **all** of the following are true:

1. Running `afteragent exec -- claude "fix the failing tests"` populates `runs.task_prompt = "fix the failing tests"`.
2. Running `afteragent exec --task "build dark mode" -- python3 scripts/agent.py` populates `runs.task_prompt = "build dark mode"` (explicit override wins).
3. Running `afteragent exec -- bash repair.sh` populates `runs.task_prompt = "bash repair.sh"` (full-command fallback).
4. A generic-mode run that edited a file but never ran tests produces a `PatternFinding` with `code="agent_edits_without_tests"`.
5. A generic-mode run where the agent edited the same file 4+ times without an intervening test produces `agent_stuck_on_file` with the correct file name in the title.
6. A generic-mode run where read/edit file sets don't overlap produces `agent_read_edit_divergence`.
7. A generic-mode run with `exit_code=1` and an assistant message saying "Fixed and ready" produces `agent_command_failure_hidden`.
8. A generic-mode run with 1 transcript event and an empty diff produces `agent_zero_meaningful_activity`.
9. All 5 detectors run in parallel within `analyze_run` and the findings list contains all relevant ones.
10. PR-oriented runs still produce their existing 6 detectors' findings — no regression in the sub-projects 1–3 test suite.
11. `afteragent enhance <run-id>` on a generic run sees the task prompt in the LLM prompt via the new `## Task prompt` section.
12. The full pytest suite is green: 209 existing + ~33 new ≈ ~242 tests.
13. **Manual dogfood acceptance:** run `afteragent exec -- claude "do some task"` against a simple non-PR project, inspect `afteragent show <run-id>` or query the `diagnoses` table, and confirm at least one generic detector produced a finding relevant to what the agent actually did. Requires Claude Code credentials.

Criterion #13 is the quality acceptance step — same pattern as sub-projects 2 and 3.

## Known followups (non-blocking)

- **Sub-project 4.5: Replay scoring for generic runs.** Define what "improved" means without PR context. Probably: fewer failing test commands, an LLM-judged diff-to-task-prompt similarity score, or something else. Real design problem, own brainstorm.
- **More generic detectors.** Candidates: `agent_ignored_failing_test_output`, `agent_never_read_task_mentioned_file`, `agent_plan_action_divergence` (requires pairing assistant_message events with subsequent tool_use events). Defer to when real usage shows which patterns matter.
- **Task prompt search / filter in `afteragent runs`.** `afteragent runs --task "dark mode"` — small CLI follow-up.
- **Threshold tuning config.** Surface `_STUCK_FILE_EDIT_THRESHOLD` etc. in `.afteragent/config.toml` once real usage shows 4 is wrong.
- **Per-detector severity weights in the merge logic.** Today all findings get equal weight.
- **Auto-task-classification.** LLM categorizes the task prompt into `{refactor, feature, bug_fix, research, devops}` and biases detector weighting.
- **Backfill of `task_prompt` on legacy runs.** Parse existing `runs.command` rows and populate the column retroactively.
