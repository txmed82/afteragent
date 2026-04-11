# Sub-Project 1: Transcript Ingestion Layer — Design

**Status:** Design approved, pending spec review
**Date:** 2026-04-10
**Owner:** Colin
**Scope:** One of five sub-projects in the AfterAgent self-improvement arc. This spec covers sub-project 1 only.

---

## Context

AfterAgent today captures stdout, stderr, git diffs, and GitHub PR JSON for agent runs, then runs six hand-coded regex/heuristic detectors in `src/afteragent/diagnostics.py` to produce findings and hardcoded-string interventions. The stated product goal — "insight into agent failures, self-improve over time, give users insight" — is honestly delivered on the first leg (capture) but not the second (learn) or third (narrate). The largest signal source that is *not* currently read is the agent's own session transcript: for Claude Code, a JSONL file at `~/.claude/projects/<cwd-slug>/<session-uuid>.jsonl` that contains structured tool calls, tool results, assistant messages, user messages, and hook events.

Sub-project 1 is the **foundation layer** for the self-improvement work. It adds a normalized transcript event schema, runner-specific parsers, pre-launch state capture, and store integration. It changes no user-visible behavior. Sub-projects 2–5 consume this foundation:

- **Sub-project 2** — LLM-driven diagnosis reads transcript events to author findings and interventions.
- **Sub-project 3** — effectiveness-driven pruning uses the same events plus the existing replay comparison.
- **Sub-project 4** — broadening past PR repair re-uses the transcript layer for runs with no GitHub context.
- **Sub-project 5** — narrative UI / weekly report surfaces transcript events to users and tells a story over them.

This decomposition was agreed before writing this spec. Each sub-project gets its own spec → plan → implementation cycle.

## Goals

1. Parse Claude Code JSONL transcripts into a normalized event schema with high fidelity.
2. Parse Codex stdout into the same schema with medium fidelity.
3. Provide a generic best-effort stdout fallback for any other runner (shell, OpenClaw, custom scripts) that produces low-fidelity events clearly marked as such.
4. Store events in a new SQLite table, queryable per run and per event kind, ordered by a monotonic sequence.
5. Preserve raw transcript sources as artifacts so downstream consumers can re-parse without re-running the agent.
6. Make the layer invisible to users: no UI, CLI, diagnostics, workflow, or intervention changes in this sub-project.
7. Never fail a run because of an ingestion problem. All parse failures become events with `kind: "parse_error"`.

## Non-goals

- No LLM calls. The opt-in `--enrich` flag discussed during brainstorming is deferred to after sub-project 2 lands LLM wiring.
- No real-time / streaming event emission. Post-hoc only.
- No UI changes. The existing UI won't render transcript events.
- No changes to `diagnostics.py`, `workflow.py`, `compare_runs`, or effectiveness scoring.
- No backfill of transcript events for historical runs. New runs only.
- No new CLI commands to query or inspect transcript events.
- No support for concurrent Claude Code runs against the same cwd beyond the mtime heuristic + a `parse_error` warning (documented known limitation).
- No event-tree threading of subagents. All events are flattened into a single sequence keyed by `run_id`.
- No token counts, model names, or cost attribution per event.
- No per-edit diff attribution. The run-level git diff is the single source of truth for "what actually landed on disk."

## Architecture

### New module

`src/afteragent/transcripts.py` owns:

- The `TranscriptEvent` dataclass.
- The `TranscriptEventKind` string enum (defined as module-level constants for easy dataclass use).
- Shared parser helpers (truncation, sequence assignment, parse-error construction).

### New table

`transcript_events` in SQLite. Separate from the existing `events` table, which stores afteragent's own lifecycle events (`capture.started`, `interventions.exported`, etc.). Those are "what afteragent did"; transcript events are "what the agent did inside the run." Different concerns, different tables, no schema overloading.

### Adapter changes

`src/afteragent/adapters.py`:

- `RunnerAdapter` base class gains two new methods:
  - `pre_launch_snapshot(self, cwd: Path) -> dict` — default returns `{}`.
  - `parse_transcript(self, run_id, artifact_dir, stdout, stderr, pre_launch_state) -> list[TranscriptEvent]` — default implementation is the generic stdout regex parser.
- `ClaudeCodeAdapter` overrides both methods with JSONL-aware implementations.
- `CodexAdapter` overrides `parse_transcript` with codex-specific stdout regex.
- `OpenClawAdapter` and `ShellAdapter` inherit the generic default for free.

### Capture changes

`src/afteragent/capture.py`:

- Before launching the subprocess, call `adapter.pre_launch_snapshot(cwd)` and stash the result in a local variable.
- In the existing finalize block (after subprocess exits), call `adapter.parse_transcript(run_id, artifact_dir, stdout, stderr, pre_launch_state)`, then write the returned events via `store.add_transcript_events(run_id, events)`.
- Pre-create `artifact_dir / "transcripts" /` so adapters have a known location to copy raw sources into.

### Store changes

`src/afteragent/store.py`:

- Additive schema migration: create `transcript_events` table + indexes.
- New methods: `add_transcript_events(run_id, events)` and `get_transcript_events(run_id, kind=None)`.

### Files touched

| File | Change |
|---|---|
| `src/afteragent/transcripts.py` | NEW |
| `src/afteragent/adapters.py` | Add `pre_launch_snapshot` + `parse_transcript` to base; override in `ClaudeCodeAdapter` and `CodexAdapter` |
| `src/afteragent/capture.py` | Two new call sites in `run_command` |
| `src/afteragent/store.py` | Migration + two new methods |
| `tests/test_transcripts.py` | NEW |
| `tests/test_adapters_claude_code.py` | NEW (fixture-driven) |
| `tests/test_adapters_codex.py` | NEW (fixture-driven) |
| `tests/test_adapters_generic.py` | NEW (fixture-driven) |
| `tests/test_capture.py` | Add integration cases for the new flow |
| `tests/fixtures/transcripts/claude_code/*.jsonl` | NEW (redacted real runs) |
| `tests/fixtures/transcripts/codex/*.txt` | NEW |
| `tests/fixtures/transcripts/generic/*.txt` | NEW |
| `scripts/e2e_matrix.sh` | Add one new e2e case |

No other files change. No UI, no diagnostics, no workflow, no CLI.

## Normalized event schema

### `TranscriptEvent` dataclass

```python
@dataclass(slots=True)
class TranscriptEvent:
    run_id: str
    sequence: int              # 0-indexed monotonic order within the run
    kind: str                  # one of TranscriptEventKind values
    tool_name: str | None      # "Read", "Edit", "Bash", etc.; None for non-tool events
    target: str | None         # filepath | command | URL | None
    inputs_summary: str        # ≤200 chars, truncated with "…"
    output_excerpt: str        # ≤500 chars, truncated with "…"
    status: str                # "success" | "error" | "unknown"
    source: str                # "claude_code_jsonl" | "codex_stdout" | "stdout_heuristic"
    timestamp: str             # ISO-8601 matching models.now_utc() format, or "" if unknown
    raw_ref: str | None        # e.g. "line:42" into raw artifact; None if not applicable
```

### Event kinds

| kind | meaning | primary source |
|---|---|---|
| `file_read` | agent read a file | Read tool; cat/head/tail detection in Bash |
| `file_edit` | agent modified or created a file | Edit/Write tools; sed/echo-redirect detection in Bash |
| `bash_command` | shell command not more specifically classified | Bash tool; bare stdout command lines |
| `test_run` | subset of `bash_command`, flagged by heuristic | pytest, jest, go test, npm test, cargo test, mocha, vitest, rspec, etc. |
| `search` | grep/glob/ripgrep operation | Grep/Glob tools; `rg`/`find` in Bash |
| `web_fetch` | URL retrieval | WebFetch tool |
| `todo_update` | task-list update | TodoWrite/TaskCreate/TaskUpdate tools |
| `subagent_call` | delegated task to a subagent | Task/Agent tools |
| `assistant_message` | non-tool assistant text (plans, summaries, explanations) | text content blocks |
| `user_message` | user-sent text incl. system reminders and tool results | user role turns |
| `hook_event` | hook fired (SessionStart, PreToolUse, etc.) | JSONL `attachment.hookEvent` blocks |
| `parse_error` | the parser itself hit a problem; stored so it's visible | parser's own error handling |
| `unknown` | something happened but couldn't be classified | fallback bucket |

### Design rationale for non-obvious choices

- **`sequence` not just `timestamp`.** Claude Code JSONL has timestamps; the generic stdout parser generally doesn't. A monotonic sequence is guaranteed to exist for every source, and downstream ordered retrieval stays simple.
- **`source` field.** Lets downstream consumers weight events by fidelity. A `file_edit` from `claude_code_jsonl` is trustworthy; the same kind from `stdout_heuristic` is a guess. Sub-project 2's LLM detector needs to know which it's looking at.
- **200 / 500-char truncation.** Enough to diagnose "did it read the failing test file" without bloating SQLite. Raw JSONL is preserved as an artifact for the rare case a detector needs full content.
- **`target` as a single string.** File path for `file_*`, command string for `bash_command`, URL for `web_fetch`. Unified so queries like "which files did the agent touch" stay trivial: `SELECT DISTINCT target FROM transcript_events WHERE kind LIKE 'file_%'`.
- **`status` tri-state with explicit `"unknown"`.** The generic parser often can't tell success from failure. Sub-project 2's detector needs to distinguish "test run we're confident failed" from "test run whose output we can't classify."
- **`raw_ref` as optional back-pointer.** Lets the future UI (sub-project 5) click through to the raw source at the right line. For `claude_code_jsonl` this is `"line:N"`; for stdout heuristics it can be `"byte:N"` or `None`.
- **`parse_error` as an event, not an exception.** Visible in the UI, countable in metrics, never crashes capture.

## Database schema

```sql
CREATE TABLE transcript_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT    NOT NULL,
    sequence   INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    tool_name  TEXT,
    target     TEXT,
    inputs_summary TEXT NOT NULL DEFAULT '',
    output_excerpt TEXT NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'unknown',
    source     TEXT    NOT NULL,
    timestamp  TEXT    NOT NULL DEFAULT '',
    raw_ref    TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX idx_transcript_events_run_seq  ON transcript_events (run_id, sequence);
CREATE INDEX idx_transcript_events_run_kind ON transcript_events (run_id, kind);
```

Migration is additive. Existing data is untouched. Expected row size is ~1 KB/event, ~50–500 events per typical run, well under SQLite's comfort zone.

## End-to-end data flow

```
User runs: afteragent exec -- claude "fix the failing tests"
  │
  ▼
capture.run_command(command=["claude", "fix ..."], cwd=/path/to/repo)
  │
  ├─ 1. select_runner_adapter(command)  →  ClaudeCodeAdapter
  │
  ├─ 2. pre_launch_state = adapter.pre_launch_snapshot(cwd)
  │       For ClaudeCodeAdapter:
  │         slug = slugify(cwd)
  │         dir  = Path.home() / ".claude" / "projects" / slug
  │         return {
  │           "claude_project_dir": dir,
  │           "pre_jsonl_files": {p: p.stat().st_mtime for p in dir.glob("*.jsonl")},
  │           "launched_at": time.time(),
  │         }
  │
  ├─ 3. subprocess launches (unchanged from today)
  │       stdout/stderr captured to files; git diff before/after captured
  │
  ├─ 4. subprocess exits
  │
  ├─ 5. events = adapter.parse_transcript(
  │             run_id, artifact_dir, stdout, stderr, pre_launch_state,
  │           )
  │         For ClaudeCodeAdapter:  JSONL discovery + parse (see next section)
  │         For CodexAdapter:       regex parse stdout
  │         For ShellAdapter/default: generic stdout heuristic
  │
  ├─ 6. store.add_transcript_events(run_id, events)
  │
  ├─ 7. raw source file(s) copied to .afteragent/runs/<id>/transcripts/
  │       session.jsonl for Claude Code
  │       (Codex and generic: stdout is already captured as a run artifact;
  │        no additional copy needed)
  │
  └─ 8. run.finalize() — existing path, unchanged
```

## Claude Code JSONL discovery

The problem: `afteragent exec -- claude "..."` launches `claude` without knowing in advance which session UUID it will create. Claude Code writes to `~/.claude/projects/<cwd-slug>/<new-uuid>.jsonl`, usually creating a new file per invocation but reusing the file for `claude --continue`.

The mechanism has three steps.

**Step 1 — pre-launch snapshot.** Before `subprocess.Popen`, `ClaudeCodeAdapter.pre_launch_snapshot(cwd)` lists the project directory and records `{Path: mtime}` for every existing `*.jsonl`, plus a wall-clock launch time. This dict travels through `capture.run_command` into the finalize block.

**Step 2 — post-exit resolution.** After subprocess exits, `parse_transcript` re-lists the same directory and computes two candidate sets:

- **New files:** paths that weren't in the pre-snapshot at all.
- **Modified files:** paths that were in the pre-snapshot but whose mtime has advanced past `launched_at`.

Modified files must count — otherwise `claude --continue` sessions fall through to the stdout fallback and lose all structured signal. This is a real case and must work in v1.

**Step 3 — pick the winner.**

- Exactly one candidate across both sets → that's ours, parse it.
- Multiple candidates → pick the one whose most recent mtime is closest to (but not later than) `subprocess_exit_time + 2s` grace window. Emit one `parse_error` event recording the ambiguity and the chosen path.
- Zero candidates → Claude Code didn't write a JSONL (bad install, cancelled before first turn, headless mode, network failure). Emit one `parse_error` event, fall through to the generic stdout parser. Run still completes, events still land, just with `source: "stdout_heuristic"`.

**Why not picking "latest mtime":** the user's OS or a background tool might touch an unrelated JSONL during the run. Closest-to-exit is a better heuristic than latest.

**What we're explicitly not doing:** no patching Claude Code to emit a known session ID, no env-var override for the JSONL path (there isn't one), no live tailing. Post-hoc directory diff is the simplest robust approach.

## Parser interface contract

Every `parse_transcript` implementation:

1. Must return events with monotonically increasing `sequence` starting at 0.
2. Must never raise. All failure modes become `parse_error` events.
3. Must produce an empty list (not `None`) if there's nothing to parse.
4. May write files into `artifact_dir / "transcripts" /`; the directory is pre-created before the adapter is called.
5. Must tag events with the correct `source` string.

This contract is what makes the layer testable in isolation: pass a fixture transcript, call `parse_transcript(...)`, assert on the returned event list. No subprocess, no filesystem magic beyond the artifact dir.

## Error handling

Nothing in the ingestion layer is allowed to fail the run. Every failure mode becomes either a `parse_error` event or a silent fallback to a lower-fidelity parser.

| Failure | Response |
|---|---|
| Claude Code JSONL not found (zero candidates) | Emit `parse_error` event, fall through to generic stdout parser |
| Claude Code JSONL malformed | Parse line-by-line, skip malformed lines, emit one `parse_error` per skipped block, continue |
| Multiple candidate JSONLs (concurrent runs) | Pick mtime-closest-to-exit, emit `parse_error` noting ambiguity |
| Codex regex parser raises | Log, emit `parse_error`, fall through to generic parser |
| Generic parser raises | Log, emit single `parse_error` event, return whatever events were collected before the exception |
| Raw transcript copy fails (disk full, permission) | Log, continue without raw artifact; normalized events still written |
| `store.add_transcript_events` fails (DB locked, etc.) | Propagate — this is genuinely broken and should surface |
| Pre-launch snapshot raises (permission on `~/.claude/projects/`) | Log, store empty `pre_launch_state`, let post-exit resolution see "zero candidates" and fall through |

Parse errors carry `kind: "parse_error"`, `output_excerpt: <error message>`, `source: <parser name>`, `status: "error"`. They appear in the same table as everything else, so downstream detectors can count them and the future UI can surface them without a separate error log.

## Testing strategy

### Unit tests — parsers (bulk of the work)

- `tests/test_transcripts.py` — `TranscriptEvent` behavior, sequence assignment, truncation of `inputs_summary`/`output_excerpt`, parse-error construction helpers.
- `tests/test_adapters_claude_code.py` — fixture-driven with `tests/fixtures/transcripts/claude_code/`:
  - `simple_edit_run.jsonl` — agent reads a file, edits it, runs tests, tests pass.
  - `ignored_review.jsonl` — agent reads unrelated files, makes edits outside the failure surface.
  - `concurrent_runs.jsonl` + sibling file to simulate the ambiguity path.
  - `malformed.jsonl` — truncated mid-line; tests error resilience.
  - `continued_session.jsonl` — pre-existing file appended to mid-test (exercises the mtime-advanced path).
- `tests/test_adapters_codex.py` — canned codex stdout samples.
- `tests/test_adapters_generic.py` — representative CLI samples (pytest, go test, npm run, bare python script).

Fixtures come from real `~/.claude/projects/` runs, redacted: absolute paths replaced with `/repo/...`, any tokens or credentials stripped. Each fixture is trimmed to 20–50 lines.

### Integration tests — capture pipeline

`tests/test_capture.py` gains:

- A case where `run_command` is called with a stub adapter whose `parse_transcript` returns a known event list; verify events land in `transcript_events` in order.
- A case using a real `ClaudeCodeAdapter` with a fake `~/.claude/projects/<slug>/` directory manipulated by the test: write a fixture JSONL mid-test to simulate a subprocess creating one, verify pre/post snapshot diff picks it up and the events land in the store.

### E2E

`scripts/e2e_matrix.sh` gains one new case: a fake `claude` shell script that writes a fixture JSONL to a temp project dir and exits 0. The matrix run verifies the full pipeline (snapshot → launch → parse → store) produces the expected event count.

### Explicitly not tested in v1

- Real Claude Code invocations in CI (requires API keys, flaky, costs money). Fixture-driven tests cover parser logic; integration tests cover the pipeline; manual dogfooding covers the rest.
- Real Codex / OpenClaw invocations, same reasoning.

## Success criteria

Sub-project 1 is done when **all** of the following are true:

1. `afteragent exec -- claude "..."` produces non-empty `transcript_events` rows with `source: "claude_code_jsonl"` and the documented event kinds, ordered by `sequence`.
2. `afteragent exec -- codex "..."` produces events with `source: "codex_stdout"`.
3. `afteragent exec -- python3 somescript.py` produces events with `source: "stdout_heuristic"` — possibly thin, but non-empty when the script prints recognizable patterns.
4. The raw Claude Code JSONL for each run is preserved at `.afteragent/runs/<id>/transcripts/session.jsonl`.
5. All existing tests still pass. New fixture, integration, and e2e tests pass.
6. No changes to the existing UI, diagnostics, workflow, or CLI surface.
7. Manual dogfood check: `afteragent exec -- claude "read the README"` lands a `file_read` event in the store whose `target` ends in `README.md`.

Criterion #7 is the acceptance test. If the single-step dogfood check fails, v1 is not done.

## Open questions

None remaining after brainstorming. All three clarifying questions (runners, richness, generic-fallback ambition) were answered before this spec was written:

- **Runners:** Claude Code (rich JSONL) + Codex (stdout parse) + generic best-effort fallback.
- **Richness:** Tier 2 — summarized tool calls with inputs/outputs truncated, plus assistant/user messages, plus raw JSONL preserved as artifact.
- **Generic fallback ambition:** regex heuristics in v1; opt-in LLM enrichment is deferred to a follow-up after sub-project 2 lands LLM wiring.
