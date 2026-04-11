# Broaden Past PR Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AfterAgent useful for non-PR agent runs (feature work, refactors, research). Capture the user's task prompt in a new `runs.task_prompt` column, add 5 rule-based detectors in a new `diagnostics_generic.py` sibling module that fire on transcript + diff signals alone, and surface the task prompt to the LLM prompts.

**Architecture:** Sibling-file pattern for detectors (new `diagnostics_generic.py` next to existing `diagnostics.py`). Adapter-per-runner pattern for task prompt extraction (`RunnerAdapter.parse_task_prompt` with `ClaudeCodeAdapter` and `CodexAdapter` overrides). Purely additive — no changes to existing 6 PR detectors, `compare_runs`, or replay scoring. Generic detectors run alongside PR detectors in `analyze_run`; both tracks always fire and produce empty lists when their inputs are empty.

**Tech Stack:** Python 3.11+, stdlib only. Consumes the `runs`, `diagnoses`, `interventions`, and `transcript_events` tables already present from sub-projects 0–3. No new dependencies.

---

## Reference documents

- **Spec:** `docs/superpowers/specs/2026-04-11-generic-diagnosis-design.md` — source of truth for design decisions.
- **Sub-project 3 plan:** `docs/superpowers/plans/2026-04-11-effectiveness-pruning.md` — sets the codebase conventions this plan follows.

## Pre-flight notes

1. **Branch:** this plan was written against `afteragent-subproject-4` branched from merged master (`53a335a`, post-v0.3.0). Do not push/pull/fetch/switch branches mid-execution. Commit locally only.
2. **Existing test count:** 209 pytest (after v0.3.0) + 28 unittest + 2 e2e. Every task's final verification must preserve "all existing tests pass."
3. **No new dependencies.** Everything uses stdlib. `pyproject.toml` stays untouched.
4. **Commit style:** imperative mood, one-line subject. Include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>` footer on every commit.
5. **Test runner:** `python3 -m pytest` from the repo root. Individual test: `python3 -m pytest tests/test_name.py::test_func -v`.
6. **Existing store machinery used by this sub-project:** `Store._ensure_column(conn, table, column, definition)` at `src/afteragent/store.py` (already used for migrations on existing tables), `Store.get_transcript_events(run_id)` from sub-project 1, `store.connection()` context manager.
7. **Existing test helpers used by this sub-project:** most test files have tmp-directory fixtures already. `tests/test_capture.py` uses `TemporaryDirectory` and a `make_paths` helper; `tests/test_diagnostics.py` has its own fixture setup. Reuse where applicable; don't redefine.

## File structure

**New files:**

```
src/afteragent/diagnostics_generic.py       # 5 generic detectors + entry point
tests/test_diagnostics_generic.py            # ~18 unit tests for the detectors
tests/test_store_task_prompt.py              # ~4 tests for the new column migration
```

**Modified files:**

```
src/afteragent/store.py                      # task_prompt column migration + set_run_task_prompt method + updated SELECTs
src/afteragent/models.py                     # RunRecord gains task_prompt field
src/afteragent/adapters.py                   # RunnerAdapter.parse_task_prompt base + CC/Codex overrides
src/afteragent/capture.py                    # run_command accepts task_prompt, resolves via three-tier fallback
src/afteragent/diagnostics.py                # analyze_run lazily imports run_generic_detectors, load_run_context adds transcript_events, build_interventions gets 5 new branches
src/afteragent/cli.py                        # exec_parser gains --task flag, dispatch passes through
src/afteragent/llm/prompts.py                # _build_base_context_block prepends task prompt section
tests/test_adapters.py                       # 8 new tests for parse_task_prompt
tests/test_capture.py                        # 3 new tests for the resolution chain
tests/test_diagnostics.py                    # 2 new tests for analyze_run orchestration
tests/test_llm_prompts.py                    # 2 new tests for the task prompt section
tests/test_cli.py                            # 2 new tests for --task flag
scripts/e2e_matrix.sh                        # Add test_diagnostics_generic.py + test_store_task_prompt.py to existing blocks
```

**Unchanged:** `workflow.py`, `ui.py`, `github.py`, `transcripts.py`, `effectiveness.py`, all `llm/*.py` except `prompts.py`, `pyproject.toml`, `README.md`.

## File responsibilities

- **`diagnostics_generic.py`** — 5 pure-function detectors taking the same `context` dict that `load_run_context` produces (extended with `transcript_events`), plus a `run_generic_detectors(context, store)` entry that calls all 5 with per-detector try/except isolation. No I/O except via the store parameter (currently unused but reserved for future-proofing).
- **`store.py`** — gains the `runs.task_prompt` migration via `_ensure_column`, a new `set_run_task_prompt(run_id, task_prompt)` method, and updated `SELECT` in `get_run` / `list_runs` / `list_previous_runs` to include the new column.
- **`models.py`** — `RunRecord.task_prompt: str | None` appended as the last field.
- **`adapters.py`** — new `RunnerAdapter.parse_task_prompt(command) -> str | None` method with default returning None; overrides on `ClaudeCodeAdapter` (recognizes `-p` / `--print` / `--print=` / trailing positional) and `CodexAdapter` (recognizes leading `run` subcommand + `-p` / `--prompt` / trailing positional).
- **`capture.py`** — `run_command` gains `task_prompt: str | None = None` parameter. Three-tier resolution: explicit kwarg → `adapter.parse_task_prompt(command)` → `shlex.join(command)`. Calls `store.set_run_task_prompt` right after `create_run`.
- **`diagnostics.py`** — `load_run_context` adds `transcript_events` to the returned context dict. `analyze_run` gains a `findings.extend(run_generic_detectors(context, store))` call after the existing 6 detector blocks. `build_interventions` gets 5 new `if` branches emitting hardcoded intervention text for the new finding codes.
- **`cli.py`** — `exec_parser` gains a `--task` flag; the `exec` dispatch passes it through to `run_command`.
- **`llm/prompts.py`** — `_build_base_context_block` prepends a `## Task prompt` section at the top when `context.run.task_prompt` is non-empty.

---

## Task 1: Store foundation for `task_prompt` column

**Files:**
- Modify: `src/afteragent/store.py`
- Modify: `src/afteragent/models.py`
- Create: `tests/test_store_task_prompt.py`

Goal: additive migration adding a nullable `task_prompt` column to the `runs` table; new `Store.set_run_task_prompt` method; `RunRecord` dataclass gains the new field; existing `SELECT` clauses updated to roundtrip the column.

- [ ] **Step 1.1: Write the failing tests**

Create `tests/test_store_task_prompt.py`:

```python
import tempfile
from pathlib import Path

import pytest

from afteragent.config import resolve_paths
from afteragent.models import RunRecord
from afteragent.store import Store


def _make_store(tmp: Path) -> Store:
    return Store(resolve_paths(tmp))


def test_run_record_roundtrips_task_prompt():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        store.create_run("run1", "claude 'fix it'", "/tmp", "2026-04-11T12:00:00Z")
        store.set_run_task_prompt("run1", "fix it")

        run = store.get_run("run1")
        assert isinstance(run, RunRecord)
        assert run.task_prompt == "fix it"


def test_set_run_task_prompt_updates_existing_row():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        store.create_run("run1", "claude 'first'", "/tmp", "2026-04-11T12:00:00Z")
        store.set_run_task_prompt("run1", "first task")
        store.set_run_task_prompt("run1", "revised task")

        run = store.get_run("run1")
        assert run.task_prompt == "revised task"


def test_existing_run_without_task_prompt_returns_none():
    with tempfile.TemporaryDirectory() as tmp_str:
        store = _make_store(Path(tmp_str))
        # Simulate a legacy run: insert row, do not call set_run_task_prompt.
        store.create_run("legacy", "some cmd", "/tmp", "2026-04-11T12:00:00Z")

        run = store.get_run("legacy")
        assert run is not None
        assert run.task_prompt is None


def test_set_run_task_prompt_migration_idempotent_on_existing_db():
    """Constructing Store twice against the same DB must not fail on the
    additive column migration."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store1 = _make_store(tmp)
        store1.create_run("run1", "cmd", "/tmp", "2026-04-11T12:00:00Z")
        store1.set_run_task_prompt("run1", "task")

        # Second construction — _ensure_column must no-op since the column exists.
        store2 = _make_store(tmp)
        run = store2.get_run("run1")
        assert run is not None
        assert run.task_prompt == "task"
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_store_task_prompt.py -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'set_run_task_prompt'` or equivalent on the first test.

- [ ] **Step 1.3: Add the `task_prompt` field to `RunRecord`**

In `src/afteragent/models.py`, find the existing `RunRecord` dataclass. Append the new field as the LAST field (to preserve construction order compatibility with existing `RunRecord(**dict(row))` calls that use the column ordering from SELECT):

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
    task_prompt: str | None  # NEW — nullable, added in sub-project 4
```

- [ ] **Step 1.4: Add the migration to `Store._init_db`**

In `src/afteragent/store.py`, find the existing `_init_db` method. After the existing `_ensure_column` calls (there should be entries for `interventions.scope`, `diagnoses.source`, `interventions.source`), add:

```python
            self._ensure_column(conn, "runs", "task_prompt", "TEXT")
```

This is idempotent — `_ensure_column` first checks `PRAGMA table_info(runs)` and returns early if the column already exists.

- [ ] **Step 1.5: Add `set_run_task_prompt` method**

In `src/afteragent/store.py`, find the existing `finish_run` method (around line 131). Add the new method right after it:

```python
    def set_run_task_prompt(self, run_id: str, task_prompt: str) -> None:
        """Record the agent's task prompt for a run. Called from
        capture.run_command after create_run — keeps the create_run
        signature stable."""
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET task_prompt = ?
                WHERE id = ?
                """,
                (task_prompt, run_id),
            )
```

- [ ] **Step 1.6: Update SELECT clauses in `get_run`, `list_runs`, `list_previous_runs`**

In `src/afteragent/store.py`, find each of these three methods. The current SELECT looks like:

```sql
SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms, summary
FROM runs
...
```

Update each to include `task_prompt` as the LAST column in the SELECT list (matching the `RunRecord` field order):

```sql
SELECT id, command, cwd, status, exit_code, created_at, finished_at, duration_ms, summary, task_prompt
FROM runs
...
```

All three methods construct `RunRecord(**dict(row))` — adding the column to the SELECT and the field to the dataclass keeps the round-trip working. Do NOT change anything else in those methods.

- [ ] **Step 1.7: Run tests**

```
python3 -m pytest tests/test_store_task_prompt.py -v
python3 -m pytest -v
```

Expected: 4 new tests pass. Full suite: 209 + 4 = 213.

- [ ] **Step 1.8: Commit**

```bash
git add src/afteragent/store.py src/afteragent/models.py tests/test_store_task_prompt.py
git commit -m "$(cat <<'EOF'
Add runs.task_prompt column and set_run_task_prompt method

Additive migration via _ensure_column adds a nullable task_prompt
column to the runs table. RunRecord dataclass gains the new field
as the last entry. Store.set_run_task_prompt updates it after
create_run so capture.run_command can populate it without changing
the existing create_run signature.

get_run, list_runs, and list_previous_runs include task_prompt in
their SELECT clauses so the column roundtrips through RunRecord.

Sub-project 4 task 1/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Adapter `parse_task_prompt` + `capture.run_command` integration

**Files:**
- Modify: `src/afteragent/adapters.py`
- Modify: `src/afteragent/capture.py`
- Modify: `tests/test_adapters.py`
- Modify: `tests/test_capture.py`

Goal: `RunnerAdapter` base class gains `parse_task_prompt(command)` method returning `None`. `ClaudeCodeAdapter` and `CodexAdapter` override with runner-specific parsing. `capture.run_command` gains optional `task_prompt` parameter and does three-tier resolution.

- [ ] **Step 2.1: Write failing tests for adapter parsing**

Append to `tests/test_adapters.py`. The top-of-file import block already imports `ClaudeCodeAdapter`, `CodexAdapter`, `ShellAdapter`, `OpenClawAdapter`, and `RunnerAdapter` from Task 5 of sub-project 1. Reuse those imports.

```python
def test_base_parse_task_prompt_returns_none():
    adapter = ShellAdapter()
    assert adapter.parse_task_prompt(["python3", "script.py"]) is None


def test_claude_adapter_parses_trailing_positional_prompt():
    adapter = ClaudeCodeAdapter()
    assert adapter.parse_task_prompt(["claude", "fix the failing test"]) == "fix the failing test"


def test_claude_adapter_parses_dash_p_flag():
    adapter = ClaudeCodeAdapter()
    assert adapter.parse_task_prompt(["claude", "-p", "build dark mode"]) == "build dark mode"


def test_claude_adapter_parses_long_print_flag():
    adapter = ClaudeCodeAdapter()
    assert adapter.parse_task_prompt(["claude", "--print", "refactor auth"]) == "refactor auth"


def test_claude_adapter_parses_equals_print_flag():
    adapter = ClaudeCodeAdapter()
    assert adapter.parse_task_prompt(["claude", "--print=quick task"]) == "quick task"


def test_claude_adapter_parses_prompt_after_permission_flag():
    adapter = ClaudeCodeAdapter()
    command = ["claude", "--dangerously-skip-permissions", "-p", "fix the bug"]
    assert adapter.parse_task_prompt(command) == "fix the bug"


def test_claude_adapter_returns_none_for_command_only():
    adapter = ClaudeCodeAdapter()
    assert adapter.parse_task_prompt(["claude"]) is None


def test_codex_adapter_parses_run_subcommand_prompt():
    adapter = CodexAdapter()
    assert adapter.parse_task_prompt(["codex", "run", "add logging"]) == "add logging"


def test_codex_adapter_parses_prompt_flag():
    adapter = CodexAdapter()
    assert adapter.parse_task_prompt(["codex", "--prompt", "summarize"]) == "summarize"


def test_codex_adapter_parses_dash_p_flag():
    adapter = CodexAdapter()
    assert adapter.parse_task_prompt(["codex", "-p", "build feature"]) == "build feature"


def test_codex_adapter_returns_none_for_command_only():
    adapter = CodexAdapter()
    assert adapter.parse_task_prompt(["codex"]) is None
```

- [ ] **Step 2.2: Run to verify it fails**

Run: `python3 -m pytest tests/test_adapters.py -v -k "parse_task_prompt"`
Expected: FAIL — `ShellAdapter` has no attribute `parse_task_prompt`.

- [ ] **Step 2.3: Add base method to `RunnerAdapter`**

In `src/afteragent/adapters.py`, find the existing `RunnerAdapter` class. Add the new method alongside existing ones like `detect`, `pre_launch_snapshot`, `parse_transcript`:

```python
    def parse_task_prompt(self, command: list[str]) -> str | None:
        """Extract the user-facing task prompt from a runner invocation.

        Default implementation returns None — callers in capture.run_command
        fall back to shlex.join(command) as the last-resort task prompt.
        Runner subclasses override this with runner-specific parsing logic.
        """
        del command
        return None
```

- [ ] **Step 2.4: Override on `ClaudeCodeAdapter`**

In `src/afteragent/adapters.py`, find the existing `ClaudeCodeAdapter` class. Add the override method alongside its existing methods (`detect`, `pre_launch_snapshot`, `parse_transcript`, `transcript_event_patterns`, etc):

```python
    def parse_task_prompt(self, command: list[str]) -> str | None:
        """Extract the task prompt from a Claude Code command.

        Supported shapes:
            claude "fix the failing test"
            claude -p "fix the failing test"
            claude --print "fix the failing test"
            claude --print="quick task"
            claude --dangerously-skip-permissions -p "fix the failing test"
            claude --dangerously-skip-permissions "fix the failing test"
        """
        if len(command) < 2:
            return None
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

- [ ] **Step 2.5: Override on `CodexAdapter`**

In `src/afteragent/adapters.py`, find the existing `CodexAdapter` class. Add:

```python
    def parse_task_prompt(self, command: list[str]) -> str | None:
        """Extract the task prompt from a Codex command.

        Supported shapes:
            codex "fix the failing test"
            codex run "fix the failing test"
            codex -p "fix the failing test"
            codex --prompt "summarize changes"
        """
        if len(command) < 2:
            return None
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

- [ ] **Step 2.6: Run adapter tests**

Run: `python3 -m pytest tests/test_adapters.py -v -k "parse_task_prompt"`
Expected: PASS — 11 new adapter tests pass.

- [ ] **Step 2.7: Write failing tests for capture integration**

Append to `tests/test_capture.py`. Reuse existing top-of-file imports where possible; the `Store`, `resolve_paths`, `run_command`, `RunnerAdapter`, `ClaudeCodeAdapter` imports should already exist from sub-projects 1 and 2. Add any missing ones to the top-of-file block.

```python
def test_run_command_auto_parses_task_prompt_from_claude_command(tmp_path: Path):
    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=tmp_path,
        adapter=ClaudeCodeAdapter(),
    )
    # The adapter-based parse returns None for a python3 command, so the
    # fallback is shlex.join(command). Verify that's what landed.
    run = store.get_run(result["run_id"])
    assert run is not None
    assert run.task_prompt is not None
    # Full-command fallback for a command with no recognizable prompt shape.
    assert "python3" in run.task_prompt


def test_run_command_explicit_task_kwarg_wins_over_adapter_parse(tmp_path: Path):
    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=tmp_path,
        adapter=ClaudeCodeAdapter(),
        task_prompt="explicit override task",
    )
    run = store.get_run(result["run_id"])
    assert run is not None
    assert run.task_prompt == "explicit override task"


def test_run_command_falls_back_to_shlex_join_when_adapter_returns_none(tmp_path: Path):
    """With ShellAdapter (base parse_task_prompt returns None), the task
    prompt falls back to shlex.join(command)."""
    from afteragent.adapters import ShellAdapter

    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=tmp_path,
        adapter=ShellAdapter(),
    )
    run = store.get_run(result["run_id"])
    assert run is not None
    # shlex.join on the command list.
    assert "python3" in run.task_prompt
    assert "print" in run.task_prompt
```

- [ ] **Step 2.8: Run to verify fails**

Run: `python3 -m pytest tests/test_capture.py -v -k "task_prompt"`
Expected: FAIL — `run_command` doesn't accept `task_prompt` kwarg yet.

- [ ] **Step 2.9: Update `capture.run_command`**

In `src/afteragent/capture.py`, find the `run_command` signature. Add the new parameter:

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
```

Immediately after the existing line `active_adapter = adapter or select_runner_adapter(cwd, command=command)`, add the three-tier resolution and a post-`create_run` call to `set_run_task_prompt`. The resolution logic goes BEFORE `create_run` so the task prompt is ready to be persisted right after the row is inserted:

```python
    active_adapter = adapter or select_runner_adapter(cwd, command=command)

    # Three-tier task prompt resolution: explicit kwarg > adapter parse > full command.
    if task_prompt is not None:
        resolved_task_prompt = task_prompt
    else:
        parsed = active_adapter.parse_task_prompt(command)
        resolved_task_prompt = parsed if parsed is not None else shlex.join(command)
```

Then find the existing `store.create_run(run_id, command_text, str(cwd), created_at, summary=summary or "Captured by afteragent exec")` call. Immediately after it, add:

```python
    store.set_run_task_prompt(run_id, resolved_task_prompt)
```

Do NOT change any other line in `run_command`. The rest of the function is unchanged.

- [ ] **Step 2.10: Run tests**

```
python3 -m pytest tests/test_adapters.py tests/test_capture.py -v
python3 -m pytest -v
```

Expected: all new adapter and capture tests pass. Full suite: 213 + 11 adapter + 3 capture = 227.

- [ ] **Step 2.11: Commit**

```bash
git add src/afteragent/adapters.py src/afteragent/capture.py tests/test_adapters.py tests/test_capture.py
git commit -m "$(cat <<'EOF'
Add parse_task_prompt to adapters, wire into capture.run_command

RunnerAdapter base class gains a parse_task_prompt method that
returns None by default. ClaudeCodeAdapter recognizes -p/--print
flags, --print= equals-form, and trailing positional prompts,
tolerating a leading --dangerously-skip-permissions flag.
CodexAdapter recognizes a leading `run` subcommand plus -p/--prompt
flags and trailing positional prompts.

capture.run_command gains a task_prompt parameter and does three-
tier resolution: explicit kwarg > adapter parse > shlex.join(command)
fallback. Stores the result via Store.set_run_task_prompt right
after create_run.

Sub-project 4 task 2/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: CLI `--task` flag on `exec` subcommand

**Files:**
- Modify: `src/afteragent/cli.py`
- Modify: `tests/test_cli.py`

Goal: `afteragent exec --task "override text" -- claude "..."` passes the explicit task prompt through to `run_command`.

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_exec_accepts_task_flag(tmp_path, monkeypatch):
    from afteragent.cli import build_parser
    parser = build_parser()

    args = parser.parse_args([
        "exec", "--task", "deploy to staging", "--",
        "python3", "-c", "print('hi')",
    ])
    assert getattr(args, "task_prompt", None) == "deploy to staging"


def test_exec_populates_task_prompt_from_claude_command_auto(tmp_path, monkeypatch, capsys):
    """When no --task flag is passed and the command is a Claude Code
    invocation, the adapter's parse_task_prompt should be used."""
    monkeypatch.chdir(tmp_path)

    from afteragent.cli import main
    from afteragent.config import resolve_paths
    from afteragent.store import Store

    # Use a no-op command so the test doesn't actually invoke claude.
    # ClaudeCodeAdapter won't be selected for python3 — so auto-detect
    # via select_runner_adapter will pick ShellAdapter and the fallback
    # will be shlex.join(command). We verify the CLI dispatch hits the
    # resolution chain either way.
    exit_code = main(["exec", "--", "python3", "-c", "print('hi')"])
    assert exit_code == 0

    store = Store(resolve_paths())
    runs = store.list_runs()
    assert len(runs) == 1
    # task_prompt is populated (either by adapter parse or fallback)
    assert runs[0].task_prompt is not None
    assert len(runs[0].task_prompt) > 0
```

- [ ] **Step 3.2: Run to verify fails**

Run: `python3 -m pytest tests/test_cli.py -v -k "task"`
Expected: FAIL on the first test with `AttributeError` or similar — the `--task` flag doesn't exist yet.

- [ ] **Step 3.3: Add the flag to `exec_parser`**

In `src/afteragent/cli.py`, find the `build_parser` function. Inside the existing `exec_parser` block (the one that contains `--summary`, `--github-repo`, `--github-pr`, `--enhance`, `--no-enhance`, `cmd`), add:

```python
    exec_parser.add_argument(
        "--task",
        dest="task_prompt",
        help="Override the auto-detected task prompt with an explicit string",
    )
```

Place it alongside the other `exec_parser.add_argument` calls, before `exec_parser.add_argument("cmd", nargs=argparse.REMAINDER)`.

- [ ] **Step 3.4: Pass the flag through in the dispatch**

In `src/afteragent/cli.py`, find the `if args.command == "exec":` block. The existing code has two `run_command` call sites (one for the github_repo branch and one for the else branch). Update BOTH calls to pass `task_prompt=getattr(args, "task_prompt", None)`:

```python
        if args.github_repo or args.github_pr:
            result = run_command(
                store,
                command,
                Path.cwd(),
                summary=args.summary,
                github_repo=args.github_repo,
                github_pr=args.github_pr,
                stream_output=not args.no_stream,
                task_prompt=getattr(args, "task_prompt", None),  # NEW
            )
        else:
            result = run_command(
                store,
                command,
                Path.cwd(),
                summary=args.summary,
                stream_output=not args.no_stream,
                task_prompt=getattr(args, "task_prompt", None),  # NEW
            )
```

Use `getattr(args, "task_prompt", None)` (not `args.task_prompt`) to be defensive in case the attribute isn't set — keeps the dispatch robust against argparse edge cases.

- [ ] **Step 3.5: Run tests**

```
python3 -m pytest tests/test_cli.py -v
python3 -m pytest -v
```

Expected: 2 new CLI tests pass. Full suite: 227 + 2 = 229.

- [ ] **Step 3.6: Commit**

```bash
git add src/afteragent/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
Add --task flag to afteragent exec

New --task flag on the exec subparser lets users explicitly
override the auto-detected task prompt. The dispatch threads it
through both run_command call sites (github_repo branch and
default branch) via task_prompt=getattr(args, "task_prompt", None).

Sub-project 4 task 3/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Generic detectors module with all 5 detectors

**Files:**
- Create: `src/afteragent/diagnostics_generic.py`
- Create: `tests/test_diagnostics_generic.py`

Goal: new sibling module with the 5 detectors as pure functions + a `run_generic_detectors(context, store)` entry point. Each detector has tests covering the fires/skips branches.

- [ ] **Step 4.1: Write the failing tests**

Create `tests/test_diagnostics_generic.py`:

```python
from dataclasses import dataclass
from typing import Any

import pytest

from afteragent.diagnostics_generic import run_generic_detectors
from afteragent.models import PatternFinding, RunRecord


@dataclass
class _StubEvent:
    """Minimal stand-in for TranscriptEventRow used by the detectors."""
    kind: str
    target: str | None = None
    output_excerpt: str = ""


def _run_record(
    run_id: str = "run1",
    exit_code: int | None = 0,
    task_prompt: str | None = "do something",
) -> RunRecord:
    return RunRecord(
        id=run_id,
        command="some command",
        cwd="/tmp",
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        created_at="2026-04-11T12:00:00Z",
        finished_at="2026-04-11T12:00:01Z",
        duration_ms=1000,
        summary="ok",
        task_prompt=task_prompt,
    )


def _context(
    run: RunRecord | None = None,
    transcript_events: list[_StubEvent] | None = None,
    changed_files: set[str] | None = None,
) -> dict:
    return {
        "run": run or _run_record(),
        "transcript_events": transcript_events or [],
        "changed_files": changed_files or set(),
    }


# ---------- run_generic_detectors entry point ----------


def test_run_generic_detectors_empty_context_returns_empty_list():
    findings = run_generic_detectors(_context(), store=None)
    assert findings == []


def test_run_generic_detectors_isolates_detector_failures(monkeypatch):
    """If one detector raises, the others still run."""
    from afteragent import diagnostics_generic

    calls: list[str] = []

    def broken_detector(context, store):
        calls.append("broken")
        raise RuntimeError("simulated failure")

    def good_detector(context, store):
        calls.append("good")
        return PatternFinding(
            code="good",
            title="good finding",
            severity="low",
            summary="x",
            evidence=[],
        )

    monkeypatch.setattr(
        diagnostics_generic,
        "_DETECTORS",
        [broken_detector, good_detector],
    )

    findings = run_generic_detectors(_context(), store=None)
    assert calls == ["broken", "good"]
    assert len(findings) == 1
    assert findings[0].code == "good"


# ---------- agent_edits_without_tests ----------


def test_edits_without_tests_fires_when_diff_has_edits_and_no_test_run():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" in codes


def test_edits_without_tests_skipped_when_pytest_was_run():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="test_run", target="pytest tests/"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" not in codes


def test_edits_without_tests_skipped_when_bash_test_command_was_run():
    """A bash_command event whose target contains a test-runner pattern
    counts as a test run (matches test_run classification heuristic)."""
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="bash_command", target="npm test --watch=false"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" not in codes


def test_edits_without_tests_skipped_when_no_edits_at_all():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/foo.py"),
        ],
        changed_files=set(),
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_edits_without_tests" not in codes


# ---------- agent_stuck_on_file ----------


def test_stuck_on_file_fires_at_threshold_of_four_edits():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    stuck = [f for f in findings if f.code == "agent_stuck_on_file"]
    assert len(stuck) == 1
    assert "foo.py" in stuck[0].title
    assert "4" in stuck[0].title


def test_stuck_on_file_resets_counter_on_test_run():
    """Test runs break the 'consecutive edits' streak."""
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="test_run", target="pytest"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    # Max streak is 2 (before test) and 2 (after test). Neither hits threshold 4.
    stuck = [f for f in findings if f.code == "agent_stuck_on_file"]
    assert stuck == []


def test_stuck_on_file_skipped_below_threshold():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
            _StubEvent(kind="file_edit", target="/repo/foo.py"),
        ],
        changed_files={"foo.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    stuck = [f for f in findings if f.code == "agent_stuck_on_file"]
    assert stuck == []


# ---------- agent_read_edit_divergence ----------


def test_read_edit_divergence_fires_when_zero_overlap():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_read", target="/repo/b.py"),
            _StubEvent(kind="file_edit", target="/repo/c.py"),
        ],
        changed_files={"c.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_read_edit_divergence" in codes


def test_read_edit_divergence_skipped_when_files_overlap():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_read", target="/repo/b.py"),
            _StubEvent(kind="file_edit", target="/repo/a.py"),
        ],
        changed_files={"a.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_read_edit_divergence" not in codes


def test_read_edit_divergence_skipped_below_activity_threshold():
    """Needs at least 2 reads AND 1 edit."""
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_edit", target="/repo/c.py"),
        ],
        changed_files={"c.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_read_edit_divergence" not in codes


# ---------- agent_command_failure_hidden ----------


def test_command_failure_hidden_fires_on_nonzero_exit_with_success_claim():
    ctx = _context(
        run=_run_record(exit_code=1),
        transcript_events=[
            _StubEvent(
                kind="assistant_message",
                output_excerpt="All tests passing, task is done.",
            ),
        ],
    )
    findings = run_generic_detectors(ctx, store=None)
    hidden = [f for f in findings if f.code == "agent_command_failure_hidden"]
    assert len(hidden) == 1
    assert "1" in hidden[0].title


def test_command_failure_hidden_skipped_on_zero_exit():
    ctx = _context(
        run=_run_record(exit_code=0),
        transcript_events=[
            _StubEvent(
                kind="assistant_message",
                output_excerpt="All tests passing.",
            ),
        ],
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_command_failure_hidden" not in codes


def test_command_failure_hidden_skipped_when_no_success_claim():
    ctx = _context(
        run=_run_record(exit_code=1),
        transcript_events=[
            _StubEvent(
                kind="assistant_message",
                output_excerpt="I hit an error and I'm not sure what to do.",
            ),
        ],
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_command_failure_hidden" not in codes


# ---------- agent_zero_meaningful_activity ----------


def test_zero_meaningful_activity_fires_on_minimal_events_and_empty_diff():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/foo.py"),
        ],
        changed_files=set(),
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_zero_meaningful_activity" in codes


def test_zero_meaningful_activity_skipped_when_activity_threshold_met():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
            _StubEvent(kind="file_read", target="/repo/b.py"),
            _StubEvent(kind="file_read", target="/repo/c.py"),
        ],
        changed_files=set(),
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_zero_meaningful_activity" not in codes


def test_zero_meaningful_activity_skipped_when_diff_has_changes():
    ctx = _context(
        transcript_events=[
            _StubEvent(kind="file_read", target="/repo/a.py"),
        ],
        changed_files={"a.py"},
    )
    findings = run_generic_detectors(ctx, store=None)
    codes = [f.code for f in findings]
    assert "agent_zero_meaningful_activity" not in codes
```

- [ ] **Step 4.2: Run to verify it fails**

Run: `python3 -m pytest tests/test_diagnostics_generic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'afteragent.diagnostics_generic'`.

- [ ] **Step 4.3: Implement `diagnostics_generic.py`**

Create `src/afteragent/diagnostics_generic.py`:

```python
from __future__ import annotations

import re

from .models import PatternFinding
from .store import Store

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STUCK_FILE_EDIT_THRESHOLD = 4
_MIN_MEANINGFUL_EVENTS = 3

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

_SUCCESS_CLAIM_PATTERNS = (
    re.compile(r"\b(fixed|done|complete|completed|ready|success(?:ful)?)\b", re.I),
    re.compile(r"\ball\s+tests\s+pass(?:ing)?\b", re.I),
    re.compile(r"\bfinished\b", re.I),
    re.compile(r"\bshould\s+(?:work|be)\s+(?:now|ready|good)\b", re.I),
)

_MEANINGFUL_KINDS = frozenset({
    "file_read",
    "file_edit",
    "bash_command",
    "test_run",
    "search",
    "web_fetch",
    "todo_update",
    "subagent_call",
})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_generic_detectors(
    context: dict,
    store: Store | None,
) -> list[PatternFinding]:
    """Run all 5 generic (non-PR) detectors against a run's context.

    Each detector is a pure function returning 0 or 1 PatternFinding.
    Per-detector try/except isolates failures so a buggy detector can't
    crash the pipeline.
    """
    findings: list[PatternFinding] = []
    for detector in _DETECTORS:
        try:
            result = detector(context, store)
        except Exception:
            continue
        if result is not None:
            findings.append(result)
    return findings


# ---------------------------------------------------------------------------
# Detector 1: agent_edits_without_tests
# ---------------------------------------------------------------------------


def _detect_edits_without_tests(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
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


# ---------------------------------------------------------------------------
# Detector 2: agent_stuck_on_file
# ---------------------------------------------------------------------------


def _detect_stuck_on_file(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
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
            f"{stuck_path}: {stuck_count} consecutive edits"
            for stuck_path, stuck_count in all_stuck[:5]
        ],
    )


# ---------------------------------------------------------------------------
# Detector 3: agent_read_edit_divergence
# ---------------------------------------------------------------------------


def _detect_read_edit_divergence(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
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


# ---------------------------------------------------------------------------
# Detector 4: agent_command_failure_hidden
# ---------------------------------------------------------------------------


def _detect_command_failure_hidden(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
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


# ---------------------------------------------------------------------------
# Detector 5: agent_zero_meaningful_activity
# ---------------------------------------------------------------------------


def _detect_zero_meaningful_activity(
    context: dict,
    store: Store | None,
) -> PatternFinding | None:
    transcript_events = context.get("transcript_events") or []
    changed_files = context.get("changed_files") or set()

    meaningful_count = sum(
        1 for e in transcript_events if e.kind in _MEANINGFUL_KINDS
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _any_event_kind(events: list, kind: str) -> bool:
    return any(e.kind == kind for e in events)


# ---------------------------------------------------------------------------
# Detector registry
# ---------------------------------------------------------------------------


_DETECTORS: list = [
    _detect_edits_without_tests,
    _detect_stuck_on_file,
    _detect_read_edit_divergence,
    _detect_command_failure_hidden,
    _detect_zero_meaningful_activity,
]
```

- [ ] **Step 4.4: Run tests**

```
python3 -m pytest tests/test_diagnostics_generic.py -v
python3 -m pytest -v
```

Expected: all ~18 generic detector tests pass. Full suite: 229 + 18 = 247.

- [ ] **Step 4.5: Commit**

```bash
git add src/afteragent/diagnostics_generic.py tests/test_diagnostics_generic.py
git commit -m "$(cat <<'EOF'
Add diagnostics_generic with 5 non-PR detectors

New src/afteragent/diagnostics_generic.py sibling module containing:

- agent_edits_without_tests: agent edited files but never ran tests
- agent_stuck_on_file: 4+ consecutive edits to one file between tests
- agent_read_edit_divergence: read/edit file sets have zero overlap
- agent_command_failure_hidden: exit!=0 with a success claim in the
  final assistant message
- agent_zero_meaningful_activity: <3 meaningful events AND empty diff

Plus a run_generic_detectors entry point that dispatches all five
with per-detector try/except isolation, matching the never-raise
contract from prior sub-projects.

18 unit tests covering each detector's fires/skips branches and
the entry point's failure isolation.

Sub-project 4 task 4/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `analyze_run` orchestration + `build_interventions` branches

**Files:**
- Modify: `src/afteragent/diagnostics.py`
- Modify: `tests/test_diagnostics.py`

Goal: `analyze_run` calls `run_generic_detectors` after the 6 existing PR detectors and extends the findings list. `load_run_context` adds `transcript_events` to the returned context. `build_interventions` gains 5 new `if` branches emitting hardcoded intervention text for the new finding codes.

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/test_diagnostics.py`:

```python
def test_analyze_run_runs_generic_detectors_alongside_pr_detectors(tmp_path):
    """A run with no GitHub context but with transcript events that match
    a generic detector should produce at least one generic finding."""
    from pathlib import Path

    from afteragent.config import resolve_paths
    from afteragent.diagnostics import analyze_run
    from afteragent.store import Store
    from afteragent.transcripts import (
        KIND_FILE_EDIT,
        SOURCE_CLAUDE_CODE_JSONL,
        TranscriptEvent,
    )

    store = Store(resolve_paths(tmp_path))
    store.create_run(
        "run1",
        "claude 'build feature'",
        str(tmp_path),
        "2026-04-11T12:00:00Z",
    )
    store.set_run_task_prompt("run1", "build feature")

    artifact_dir = store.run_artifact_dir("run1")
    (artifact_dir / "stdout.log").write_text("")
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
    store.finish_run("run1", "passed", 0, "2026-04-11T12:00:01Z", 1000, "ok")

    # Add a file_edit transcript event — triggers agent_edits_without_tests.
    store.add_transcript_events(
        "run1",
        [
            TranscriptEvent(
                run_id="run1",
                sequence=0,
                kind=KIND_FILE_EDIT,
                tool_name="Edit",
                target="/repo/foo.py",
                source=SOURCE_CLAUDE_CODE_JSONL,
                raw_ref="line:1",
                timestamp="2026-04-11T12:00:00Z",
            ),
        ],
    )

    findings, interventions = analyze_run(store, "run1")
    codes = [f.code for f in findings]
    # The generic detector fires because there's an edit but no test run.
    assert "agent_edits_without_tests" in codes


def test_analyze_run_generic_detectors_isolated_from_pr_detectors(tmp_path):
    """Generic detector failures shouldn't break PR detector findings."""
    from pathlib import Path
    from unittest.mock import patch

    from afteragent.config import resolve_paths
    from afteragent.diagnostics import analyze_run
    from afteragent.store import Store

    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-11T12:00:00Z")

    artifact_dir = store.run_artifact_dir("run1")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-11T12:00:01Z", 1000, "ok")

    # Patch run_generic_detectors to raise — analyze_run must still complete.
    with patch(
        "afteragent.diagnostics_generic.run_generic_detectors",
        side_effect=RuntimeError("simulated generic detector crash"),
    ):
        # Current behavior: analyze_run catches any exception from
        # run_generic_detectors and falls back to PR findings only.
        findings, interventions = analyze_run(store, "run1")
        # No exception raised. Findings list is whatever the PR detectors
        # produced (likely empty on this minimal run).
        assert isinstance(findings, list)
```

- [ ] **Step 5.2: Run to verify fails**

Run: `python3 -m pytest tests/test_diagnostics.py -v -k "generic"`
Expected: FAIL — `analyze_run` doesn't call the generic detectors yet, so `agent_edits_without_tests` won't appear in findings.

- [ ] **Step 5.3: Update `load_run_context` to include `transcript_events`**

In `src/afteragent/diagnostics.py`, find the `load_run_context` function (around line 262). The existing return statement looks like:

```python
    return {
        "run": run,
        "stdout": stdout,
        "stderr": stderr,
        "gh_context": gh_context,
        "changed_files": changed_files,
        "pr_changed_files": pr_changed_files,
        "analysis_files": analysis_files,
        "failure_files": failure_files,
        "failure_signatures": failure_signatures,
        "unresolved_comment_paths": unresolved_paths,
    }
```

Before the return, add one line to load transcript events:

```python
    transcript_events = store.get_transcript_events(run_id)
```

And add `"transcript_events": transcript_events,` to the returned dict as the LAST key. The full updated return:

```python
    transcript_events = store.get_transcript_events(run_id)
    return {
        "run": run,
        "stdout": stdout,
        "stderr": stderr,
        "gh_context": gh_context,
        "changed_files": changed_files,
        "pr_changed_files": pr_changed_files,
        "analysis_files": analysis_files,
        "failure_files": failure_files,
        "failure_signatures": failure_signatures,
        "unresolved_comment_paths": unresolved_paths,
        "transcript_events": transcript_events,
    }
```

- [ ] **Step 5.4: Update `analyze_run` to call generic detectors**

In `src/afteragent/diagnostics.py`, find the `analyze_run` function (around line 15). It currently has the 6 existing detector blocks that append to `findings`, followed by a call to `build_interventions(findings)` and the persist logic. After the last PR detector block but BEFORE `interventions = build_interventions(findings)`, add:

```python
    # Generic (non-PR) detectors — run alongside the PR-oriented ones.
    # Per-detector failures are isolated inside run_generic_detectors, but
    # wrap the whole call in try/except so a broken import or module-level
    # bug can't break analyze_run.
    try:
        from .diagnostics_generic import run_generic_detectors
        findings.extend(run_generic_detectors(context, store))
    except Exception:
        pass
```

The lazy import inside the try/except avoids any circular-import risk and matches the "never break the run" contract.

- [ ] **Step 5.5: Add hardcoded intervention branches to `build_interventions`**

In `src/afteragent/diagnostics.py`, find `build_interventions` (around line 131). It contains a chain of `if "<code>" in codes:` branches emitting hardcoded `Intervention` objects. Add 5 new branches at the END of the existing chain, before the `return interventions` line:

```python
    if "agent_edits_without_tests" in codes:
        interventions.append(
            Intervention(
                type="instruction_patch",
                title="Require a test run after edits",
                target="repo_instructions",
                content=(
                    "Before declaring a task complete, run the project's test "
                    "command (pytest, npm test, go test, etc.) and summarize "
                    "the result. Never finish an edit cycle without verifying it."
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
                    "After each file edit in this run, run the relevant test "
                    "command and quote the outcome in your next message before "
                    "moving to the next edit."
                ),
                scope="pr",
            )
        )

    if "agent_stuck_on_file" in codes:
        interventions.append(
            Intervention(
                type="runtime_guardrail",
                title="Break edit loops with test runs",
                target="runner_policy",
                content=(
                    "If the agent edits the same file more than 3 times in a row "
                    "without running tests in between, stop editing and run the "
                    "relevant test command before continuing."
                ),
                scope="pr",
            )
        )
        interventions.append(
            Intervention(
                type="prompt_patch",
                title="Run tests between repeated edits",
                target="task_prompt",
                content=(
                    "If you find yourself editing the same file multiple times, "
                    "stop and run the relevant test command. Quote the result "
                    "before deciding your next edit — don't guess."
                ),
                scope="pr",
            )
        )

    if "agent_read_edit_divergence" in codes:
        interventions.append(
            Intervention(
                type="instruction_patch",
                title="Read the files you intend to edit",
                target="repo_instructions",
                content=(
                    "Before editing a file, read it. If you read a file that is "
                    "not the file you are about to edit, summarize why that "
                    "reading is relevant to the edit you plan to make."
                ),
                scope="pr",
            )
        )

    if "agent_command_failure_hidden" in codes:
        interventions.append(
            Intervention(
                type="instruction_patch",
                title="Verify success before claiming it",
                target="repo_instructions",
                content=(
                    "Before declaring a task done, run the relevant verification "
                    "command (tests, build, lint) and quote the exit code. Never "
                    "claim completion when the most recent command failed."
                ),
                scope="pr",
            )
        )
        interventions.append(
            Intervention(
                type="prompt_patch",
                title="Quote the exit code before claiming success",
                target="task_prompt",
                content=(
                    "Before saying the task is fixed, done, or ready, run the "
                    "relevant verification command and quote its exit code. If "
                    "the exit code is non-zero, the task is not done."
                ),
                scope="pr",
            )
        )

    if "agent_zero_meaningful_activity" in codes:
        interventions.append(
            Intervention(
                type="prompt_patch",
                title="Clarify task intent before acting",
                target="task_prompt",
                content=(
                    "If the task is unclear, ask a clarifying question before "
                    "taking any action. If the task is clear but requires no "
                    "code changes, say so explicitly and explain why."
                ),
                scope="pr",
            )
        )
```

The indentation should match the existing `if` blocks in `build_interventions` (typically 4-space indent inside the function body).

- [ ] **Step 5.6: Run tests**

```
python3 -m pytest tests/test_diagnostics.py -v
python3 -m pytest -v
```

Expected: new integration tests pass + existing diagnostics tests still pass. Full suite: 247 + 2 = 249.

- [ ] **Step 5.7: Commit**

```bash
git add src/afteragent/diagnostics.py tests/test_diagnostics.py
git commit -m "$(cat <<'EOF'
Wire generic detectors into analyze_run and build_interventions

load_run_context adds transcript_events to the returned context
dict so the new detectors have access to the sub-project 1 signal.

analyze_run extends the findings list with run_generic_detectors
output via a lazy import wrapped in try/except. The wrap is belt-
and-suspenders: per-detector failures are already isolated inside
run_generic_detectors, but a broken import or module-level bug
can't break analyze_run either.

build_interventions gains 5 new if branches emitting hardcoded
intervention text for the new finding codes (second-person
imperative, matching the existing 6 PR-oriented branches).

Sub-project 4 task 5/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: LLM prompt `## Task prompt` section

**Files:**
- Modify: `src/afteragent/llm/prompts.py`
- Modify: `tests/test_llm_prompts.py`

Goal: `_build_base_context_block` prepends a `## Task prompt` section at the top when `context.run.task_prompt` is non-empty. Both `build_diagnosis_prompt` and `build_interventions_prompt` benefit automatically because they both call `_build_base_context_block`.

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/test_llm_prompts.py`. The existing `_seed_run_with_artifacts` helper from sub-projects 1-3 is still used; reuse it. Make sure the test seeds a task_prompt on the run.

```python
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
```

- [ ] **Step 6.2: Run to verify fails**

Run: `python3 -m pytest tests/test_llm_prompts.py -v -k "task_prompt"`
Expected: FAIL — the `## Task prompt` section isn't produced yet.

- [ ] **Step 6.3: Update `_build_base_context_block`**

In `src/afteragent/llm/prompts.py`, find the `_build_base_context_block` function. The current version starts by appending run metadata and other sections to a `sections: list[str]` list. Find the line where `sections` is first initialized:

```python
def _build_base_context_block(
    context: DiagnosisContext,
    include_findings_header: str | None,
) -> str:
    sections: list[str] = []

    sections.append(
        f"## Run metadata\n"
        ...
    )
```

Right after `sections: list[str] = []` and BEFORE the first `sections.append(...)` call for run metadata, add the task prompt block:

```python
    sections: list[str] = []

    # NEW: task prompt section at the top when available.
    if context.run.task_prompt:
        sections.append(f"## Task prompt\n\n{context.run.task_prompt}")
```

The rest of `_build_base_context_block` (run metadata, findings, transcript events, diff, stdout/stderr, github summary) stays exactly as before. Do NOT change anything else in the function.

- [ ] **Step 6.4: Run tests**

```
python3 -m pytest tests/test_llm_prompts.py -v
python3 -m pytest -v
```

Expected: new tests pass. Existing prompt tests still pass — the task_prompt section just appears at the top when set, otherwise the prompt is unchanged. Full suite: 249 + 2 = 251.

- [ ] **Step 6.5: Commit**

```bash
git add src/afteragent/llm/prompts.py tests/test_llm_prompts.py
git commit -m "$(cat <<'EOF'
Prepend Task prompt section to LLM prompts when set

_build_base_context_block now prepends a ## Task prompt section
at the top of the user message when context.run.task_prompt is
non-empty. Both build_diagnosis_prompt and build_interventions_prompt
benefit automatically.

Adds ~20-100 tokens to the prompt. Well within the 25k budget.
Zero effect on runs where task_prompt is NULL (legacy runs).

Sub-project 4 task 6/7.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: E2E matrix + manual dogfood acceptance

**Files:**
- Modify: `scripts/e2e_matrix.sh`

Goal: append the new test files to the existing e2e matrix blocks. Verify the full matrix passes. Attempt the manual dogfood acceptance step per the spec's success criterion #13.

- [ ] **Step 7.1: Inspect the current matrix**

Run: `cat scripts/e2e_matrix.sh`
Find the existing `Running LLM diagnosis tests...` block and the `Running transcript ingestion tests...` block.

- [ ] **Step 7.2: Append the new test files**

Edit `scripts/e2e_matrix.sh`. Find the `Running LLM diagnosis tests...` block and add `tests/test_diagnostics_generic.py` to its pytest invocation's file list. Find the `Running transcript ingestion tests...` block and add `tests/test_store_task_prompt.py` to it.

Exact edits:

For the LLM diagnosis block, the current file list ends with `tests/test_effectiveness.py tests/test_cli.py` (from sub-project 3). Add `tests/test_diagnostics_generic.py` before `tests/test_cli.py`:

```bash
    tests/test_effectiveness.py \
    tests/test_diagnostics_generic.py \
    tests/test_cli.py
```

For the transcript ingestion block, the current file list ends with `tests/test_capture.py`. Add `tests/test_store_task_prompt.py` after it:

```bash
    tests/test_capture.py \
    tests/test_store_task_prompt.py
```

- [ ] **Step 7.3: Run the matrix**

Run: `bash scripts/e2e_matrix.sh`
Expected: all blocks pass. The LLM diagnosis block gains 18 tests (generic detectors). The transcript ingestion block gains 4 tests (store task_prompt).

- [ ] **Step 7.4: Commit**

```bash
git add scripts/e2e_matrix.sh
git commit -m "$(cat <<'EOF'
Add sub-project 4 tests to e2e matrix

test_diagnostics_generic.py joins the LLM diagnosis block
alongside sub-project 3's effectiveness tests (same integration
surface). test_store_task_prompt.py joins the transcript ingestion
block since it's a store migration test.

Sub-project 4 task 7/7 (code).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7.5: Manual dogfood acceptance check**

Structural verification steps (all should pass without real LLM credentials):

```bash
# 1. Confirm the column exists and populates.
cd /tmp && rm -rf afteragent-sp4-dogfood && mkdir afteragent-sp4-dogfood
cd afteragent-sp4-dogfood && git init -q
afteragent exec -- python3 -c "print('hi')"
sqlite3 .afteragent/afteragent.sqlite3 "SELECT id, task_prompt FROM runs;"
# Expected: one row, task_prompt is "python3 -c 'print('\''hi'\'')" (shlex.join fallback)

# 2. Confirm --task flag overrides the fallback.
afteragent exec --task "deploy to staging" -- bash -c "true"
sqlite3 .afteragent/afteragent.sqlite3 "SELECT id, task_prompt FROM runs ORDER BY created_at DESC LIMIT 1;"
# Expected: latest row has task_prompt = "deploy to staging"

# 3. Confirm a generic detector fires. This requires a run with an edit
#    and no test — closest simulation without real claude is a script that
#    touches a file but runs no tests.
cat > touch_script.py <<'PY'
import pathlib
pathlib.Path("foo.txt").write_text("edit\n")
PY
afteragent exec --task "edit foo" -- python3 touch_script.py
# Get the run id and check findings:
RUN_ID=$(sqlite3 .afteragent/afteragent.sqlite3 "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1;")
sqlite3 .afteragent/afteragent.sqlite3 "SELECT code FROM diagnoses WHERE run_id='$RUN_ID';"
# Expected: the diagnoses table may be empty because this is a shell-script
# run with no transcript_events (no Claude Code session). The generic
# detectors that depend on transcript events won't fire. This is OK —
# the dogfood with real claude is the meaningful test.
```

Quality verification step (requires real LLM credentials):

```bash
# 4. Real Claude Code run with a task that fires a generic detector.
#    "Edit a file but don't test it" is the easiest trigger.
cd /tmp/afteragent-sp4-dogfood && rm -rf .afteragent
afteragent exec --task "edit README without running tests" -- \
  claude -p --dangerously-skip-permissions "edit the README.md file to add a hello world section, don't run any tests"

RUN_ID=$(sqlite3 .afteragent/afteragent.sqlite3 "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1;")

# Check that task_prompt landed:
sqlite3 .afteragent/afteragent.sqlite3 "SELECT task_prompt FROM runs WHERE id='$RUN_ID';"
# Expected: "edit README without running tests"

# Check that a generic detector fired:
sqlite3 .afteragent/afteragent.sqlite3 "SELECT code, title FROM diagnoses WHERE run_id='$RUN_ID';"
# Expected: at least one row with code = "agent_edits_without_tests"
```

If step 4 requires real credentials and they're unavailable, mark the dogfood as "structurally verified, quality inspection pending" — same pattern as sub-projects 2 and 3.

- [ ] **Step 7.6: Tag sub-project 4 complete**

```bash
# Only run this if all structural verification passed.
git tag subproject-4-complete
```

---

## Self-review checklist (plan author)

**Spec coverage:**
- [x] Goal 1 (task prompt capture, three-tier resolution, runs.task_prompt column): Tasks 1, 2, 3
- [x] Goal 2 (RunnerAdapter.parse_task_prompt base + CC/Codex overrides): Task 2
- [x] Goal 3 (5 generic detectors in new sibling module): Task 4
- [x] Goal 4 (analyze_run calls generic track alongside PR track): Task 5
- [x] Goal 5 (LLM prompt task_prompt section): Task 6
- [x] Goal 6 (additive runs.task_prompt column migration): Task 1
- [x] Goal 7 (never-break-the-run contract — per-detector try/except + wrap in analyze_run): Tasks 4 and 5
- [x] Non-goals respected: no changes to compare_runs, replay_run, github.py, or existing PR detectors; no new intervention types; no task classification; no UI changes; no threshold config; no LLM-based generic detectors

**Placeholder scan:**
- No "TBD" / "TODO" / "implement later" / "similar to Task N" / "fill in details"
- Every test has full Python code
- Every implementation step has full Python code
- Every commit message is fully written

**Type consistency:**
- `RunRecord` field order matches between Task 1 (definition), Task 2 (test assertions), Task 5 (test seeding), Task 6 (prompt integration). `task_prompt` is the last field throughout.
- `parse_task_prompt(command: list[str]) -> str | None` signature matches between Task 2 definition, Task 2 tests, and Task 2 `capture.run_command` integration.
- `run_generic_detectors(context: dict, store: Store | None) -> list[PatternFinding]` matches Task 4 definition, Task 4 tests, Task 5 integration.
- `_DETECTORS` is the module-level registry variable in Task 4 and is monkeypatched in Task 4's failure-isolation test.
- Finding codes (`agent_edits_without_tests`, `agent_stuck_on_file`, `agent_read_edit_divergence`, `agent_command_failure_hidden`, `agent_zero_meaningful_activity`) are consistent across Task 4 (detectors), Task 4 (tests), Task 5 (build_interventions branches), and the spec's success criteria.

**Known plan imperfections (acknowledged, not blocking):**
- Task 7's manual dogfood step 4 requires real Claude Code credentials to meaningfully verify. Same pattern as sub-projects 2 and 3 — documented as "structurally verified, quality inspection pending" when credentials are unavailable.
- The `_StubEvent` dataclass in Task 4's tests is a minimal stand-in for `TranscriptEventRow`. It doesn't derive from the real class — the detectors only read `.kind`, `.target`, `.output_excerpt`, so duck-typing is sufficient for the unit tests.
- Task 5's `analyze_run` test uses real `TranscriptEvent` instances via `store.add_transcript_events`, since the integration test needs the full store round-trip.
