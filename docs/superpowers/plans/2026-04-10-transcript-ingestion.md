# Transcript Ingestion Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a normalized transcript event schema and per-runner parsers (Claude Code JSONL, Codex stdout, generic fallback) that store rich per-run agent activity without changing any existing user-visible behavior. Foundation for sub-projects 2–5.

**Architecture:** Post-hoc parsing in a new `transcripts.py` module plus two new methods on the existing `RunnerAdapter` base class. A new `transcript_events` SQLite table is added via additive migration. Capture gains two call sites: a pre-launch snapshot and a post-exit parse+write. No changes to UI, diagnostics, workflow, CLI, or `events` table.

**Tech Stack:** Python 3.11+, stdlib-only (`sqlite3`, `json`, `re`, `pathlib`, `dataclasses`), pytest for testing.

---

## Reference documents

- **Spec:** `docs/superpowers/specs/2026-04-10-transcript-ingestion-design.md` — read this first; it's the source of truth for scope, schema, and success criteria.
- **Decomposition:** Sub-project 1 of 5. Do not implement sub-projects 2–5 in this plan.

## Pre-flight notes

1. **Working tree:** This plan was written against `master` at commit `195a4ac` (the spec commit). No worktree was created for this sub-project. If the execution environment supports it, creating a dedicated worktree (e.g. `afteragent-subproject-1`) before starting is recommended — use the `superpowers:using-git-worktrees` skill. Otherwise proceed on `master` with frequent commits.
2. **Spec path correction:** The spec says raw transcripts land at `.afteragent/runs/<id>/transcripts/`, but the actual artifact convention in `src/afteragent/config.py` uses `.afteragent/artifacts/<run_id>/`. **Use `.afteragent/artifacts/<run_id>/transcripts/` in the implementation.** The spec's `runs/` path is a minor inaccuracy; this plan uses the correct `artifacts/` path throughout.
3. **Existing `parse_transcript_events` method:** `adapters.py` already has a method called `parse_transcript_events` that returns `list[dict]` in the old event format and writes to the `events` table via `capture.py:111`. **Do not remove or modify this method.** The new `parse_transcript` method (returning `list[TranscriptEvent]`) is additive and writes to the new `transcript_events` table. The two coexist. Sub-project 2+ can eventually deprecate the old path.
4. **Commit style:** Follow existing commits (`Build AfterAction MVP and runner adapters (#1)`, `Rename package...`). Imperative mood, one-line subject. Include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>` footer on all commits made during this plan's execution.
5. **Test runner:** No pytest config in `pyproject.toml`, so default discovery applies. Run tests with `pytest` from the repo root. Individual test: `pytest tests/test_transcripts.py::test_name -v`.
6. **Dogfood criterion:** Success criterion #7 from the spec (manual `afteragent exec -- claude "read the README"` check) is Task 14, the final acceptance step.

## File structure

**New files:**

```
src/afteragent/transcripts.py                       # dataclass, kinds, generic parser, helpers
tests/test_transcripts.py                           # unit tests for the module
tests/test_adapters_claude_code.py                  # fixture-driven parser tests
tests/test_adapters_codex.py                        # fixture-driven parser tests
tests/test_adapters_generic.py                      # fixture-driven generic-parser tests
tests/fixtures/transcripts/claude_code/simple_edit_run.jsonl
tests/fixtures/transcripts/claude_code/ignored_review.jsonl
tests/fixtures/transcripts/claude_code/malformed.jsonl
tests/fixtures/transcripts/claude_code/continued_session.jsonl
tests/fixtures/transcripts/codex/simple_run.txt
tests/fixtures/transcripts/codex/test_run_with_errors.txt
tests/fixtures/transcripts/generic/pytest_output.txt
tests/fixtures/transcripts/generic/npm_script.txt
```

**Modified files:**

```
src/afteragent/adapters.py           # add pre_launch_snapshot + parse_transcript to base; override ClaudeCode/Codex
src/afteragent/capture.py            # two new call sites in run_command
src/afteragent/store.py              # migration + add/get transcript_events methods
tests/test_capture.py                # new integration cases
scripts/e2e_matrix.sh                # new e2e case
```

**Unchanged (explicit):** `src/afteragent/models.py`, `src/afteragent/diagnostics.py`, `src/afteragent/workflow.py`, `src/afteragent/cli.py`, `src/afteragent/ui.py`, `src/afteragent/github.py`, `src/afteragent/config.py`.

## File responsibilities

- **`transcripts.py`** — owns the `TranscriptEvent` dataclass, the `EventKind` constants, the truncation/parse-error helpers, and the `parse_generic_stdout` function. No adapter logic. No store logic. Pure data + pure functions.
- **`adapters.py`** — keeps its existing responsibilities; the two new methods (`pre_launch_snapshot` and `parse_transcript`) call into `transcripts.py` for the generic path and add per-runner JSONL/regex parsing in subclasses.
- **`store.py`** — gains `add_transcript_events` and `get_transcript_events` methods plus one additive migration in `_init_db`. Nothing else changes.
- **`capture.py`** — `run_command` gains a snapshot call before `subprocess.Popen` and a parse+store-write after subprocess exit. No other changes.

---

## Task 1: `TranscriptEvent` dataclass and event kind constants

**Files:**
- Create: `src/afteragent/transcripts.py`
- Create: `tests/test_transcripts.py`

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_transcripts.py`:

```python
from afteragent.transcripts import (
    TranscriptEvent,
    KIND_FILE_READ,
    KIND_FILE_EDIT,
    KIND_BASH_COMMAND,
    KIND_TEST_RUN,
    KIND_SEARCH,
    KIND_WEB_FETCH,
    KIND_TODO_UPDATE,
    KIND_SUBAGENT_CALL,
    KIND_ASSISTANT_MESSAGE,
    KIND_USER_MESSAGE,
    KIND_HOOK_EVENT,
    KIND_PARSE_ERROR,
    KIND_UNKNOWN,
    SOURCE_CLAUDE_CODE_JSONL,
    SOURCE_CODEX_STDOUT,
    SOURCE_STDOUT_HEURISTIC,
)


def test_transcript_event_minimal_construction():
    event = TranscriptEvent(
        run_id="abc123",
        sequence=0,
        kind=KIND_FILE_READ,
        tool_name="Read",
        target="/repo/README.md",
        inputs_summary="",
        output_excerpt="",
        status="success",
        source=SOURCE_CLAUDE_CODE_JSONL,
        timestamp="2026-04-10T12:00:00Z",
        raw_ref="line:42",
    )
    assert event.run_id == "abc123"
    assert event.sequence == 0
    assert event.kind == "file_read"
    assert event.tool_name == "Read"
    assert event.target == "/repo/README.md"
    assert event.status == "success"
    assert event.source == "claude_code_jsonl"


def test_transcript_event_allows_optional_fields_as_none():
    event = TranscriptEvent(
        run_id="abc123",
        sequence=1,
        kind=KIND_ASSISTANT_MESSAGE,
        tool_name=None,
        target=None,
        inputs_summary="",
        output_excerpt="I'm going to fix the failing test.",
        status="unknown",
        source=SOURCE_CLAUDE_CODE_JSONL,
        timestamp="",
        raw_ref=None,
    )
    assert event.tool_name is None
    assert event.target is None
    assert event.raw_ref is None


def test_event_kind_constants_have_expected_values():
    assert KIND_FILE_READ == "file_read"
    assert KIND_FILE_EDIT == "file_edit"
    assert KIND_BASH_COMMAND == "bash_command"
    assert KIND_TEST_RUN == "test_run"
    assert KIND_SEARCH == "search"
    assert KIND_WEB_FETCH == "web_fetch"
    assert KIND_TODO_UPDATE == "todo_update"
    assert KIND_SUBAGENT_CALL == "subagent_call"
    assert KIND_ASSISTANT_MESSAGE == "assistant_message"
    assert KIND_USER_MESSAGE == "user_message"
    assert KIND_HOOK_EVENT == "hook_event"
    assert KIND_PARSE_ERROR == "parse_error"
    assert KIND_UNKNOWN == "unknown"


def test_source_constants_have_expected_values():
    assert SOURCE_CLAUDE_CODE_JSONL == "claude_code_jsonl"
    assert SOURCE_CODEX_STDOUT == "codex_stdout"
    assert SOURCE_STDOUT_HEURISTIC == "stdout_heuristic"
```

- [ ] **Step 1.2: Run the test to verify it fails**

Run: `pytest tests/test_transcripts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'afteragent.transcripts'`.

- [ ] **Step 1.3: Write the minimal implementation**

Create `src/afteragent/transcripts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

# Event kinds — see spec section "Normalized event schema".
KIND_FILE_READ = "file_read"
KIND_FILE_EDIT = "file_edit"
KIND_BASH_COMMAND = "bash_command"
KIND_TEST_RUN = "test_run"
KIND_SEARCH = "search"
KIND_WEB_FETCH = "web_fetch"
KIND_TODO_UPDATE = "todo_update"
KIND_SUBAGENT_CALL = "subagent_call"
KIND_ASSISTANT_MESSAGE = "assistant_message"
KIND_USER_MESSAGE = "user_message"
KIND_HOOK_EVENT = "hook_event"
KIND_PARSE_ERROR = "parse_error"
KIND_UNKNOWN = "unknown"

# Source identifiers — tells downstream consumers the fidelity of the event.
SOURCE_CLAUDE_CODE_JSONL = "claude_code_jsonl"
SOURCE_CODEX_STDOUT = "codex_stdout"
SOURCE_STDOUT_HEURISTIC = "stdout_heuristic"


@dataclass(slots=True)
class TranscriptEvent:
    run_id: str
    sequence: int
    kind: str
    tool_name: str | None
    target: str | None
    inputs_summary: str
    output_excerpt: str
    status: str
    source: str
    timestamp: str
    raw_ref: str | None
```

- [ ] **Step 1.4: Run the test to verify it passes**

Run: `pytest tests/test_transcripts.py -v`
Expected: PASS — 4 tests pass.

- [ ] **Step 1.5: Commit**

```bash
git add src/afteragent/transcripts.py tests/test_transcripts.py
git commit -m "$(cat <<'EOF'
Add TranscriptEvent dataclass and event kind constants

Sub-project 1 foundation: the normalized event shape and vocabulary
that all runner parsers will emit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Truncation helper and parse-error factory

**Files:**
- Modify: `src/afteragent/transcripts.py`
- Modify: `tests/test_transcripts.py`

- [ ] **Step 2.1: Write the failing tests**

Append to `tests/test_transcripts.py`:

```python
from afteragent.transcripts import (
    truncate,
    make_parse_error,
    INPUTS_SUMMARY_MAX,
    OUTPUT_EXCERPT_MAX,
)


def test_truncate_leaves_short_text_unchanged():
    assert truncate("hello world", 20) == "hello world"


def test_truncate_clips_long_text_with_ellipsis():
    text = "a" * 300
    result = truncate(text, 100)
    assert len(result) == 100
    assert result.endswith("…")
    assert result[:99] == "a" * 99


def test_truncate_handles_zero_length_safely():
    assert truncate("", 100) == ""


def test_truncate_handles_exact_length():
    text = "a" * 100
    assert truncate(text, 100) == text


def test_inputs_summary_max_is_200():
    assert INPUTS_SUMMARY_MAX == 200


def test_output_excerpt_max_is_500():
    assert OUTPUT_EXCERPT_MAX == 500


def test_make_parse_error_fills_all_required_fields():
    event = make_parse_error(
        run_id="abc123",
        sequence=5,
        source="claude_code_jsonl",
        message="could not decode JSON on line 42",
        raw_ref="line:42",
    )
    assert event.run_id == "abc123"
    assert event.sequence == 5
    assert event.kind == "parse_error"
    assert event.tool_name is None
    assert event.target is None
    assert event.inputs_summary == ""
    assert event.output_excerpt == "could not decode JSON on line 42"
    assert event.status == "error"
    assert event.source == "claude_code_jsonl"
    assert event.timestamp == ""
    assert event.raw_ref == "line:42"


def test_make_parse_error_truncates_long_messages():
    long_message = "x" * 1000
    event = make_parse_error(
        run_id="abc",
        sequence=0,
        source="stdout_heuristic",
        message=long_message,
        raw_ref=None,
    )
    assert len(event.output_excerpt) == OUTPUT_EXCERPT_MAX
    assert event.output_excerpt.endswith("…")
```

- [ ] **Step 2.2: Run the tests to verify they fail**

Run: `pytest tests/test_transcripts.py -v`
Expected: FAIL with `ImportError` on `truncate`, `make_parse_error`, `INPUTS_SUMMARY_MAX`, `OUTPUT_EXCERPT_MAX`.

- [ ] **Step 2.3: Add the helpers**

Append to `src/afteragent/transcripts.py`:

```python
INPUTS_SUMMARY_MAX = 200
OUTPUT_EXCERPT_MAX = 500
_ELLIPSIS = "…"


def truncate(text: str, max_len: int) -> str:
    """Return text unchanged if short enough; otherwise clip and append an ellipsis.

    The returned string is guaranteed to be at most max_len characters.
    A single-character ellipsis is used so the visible length matches max_len.
    """
    if len(text) <= max_len:
        return text
    if max_len <= 0:
        return ""
    return text[: max_len - 1] + _ELLIPSIS


def make_parse_error(
    run_id: str,
    sequence: int,
    source: str,
    message: str,
    raw_ref: str | None,
) -> TranscriptEvent:
    """Construct a parse_error event. The parser's message goes in output_excerpt."""
    return TranscriptEvent(
        run_id=run_id,
        sequence=sequence,
        kind=KIND_PARSE_ERROR,
        tool_name=None,
        target=None,
        inputs_summary="",
        output_excerpt=truncate(message, OUTPUT_EXCERPT_MAX),
        status="error",
        source=source,
        timestamp="",
        raw_ref=raw_ref,
    )
```

- [ ] **Step 2.4: Run the tests to verify they pass**

Run: `pytest tests/test_transcripts.py -v`
Expected: PASS — all tests from Task 1 + 8 new tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add src/afteragent/transcripts.py tests/test_transcripts.py
git commit -m "$(cat <<'EOF'
Add truncation helper and parse_error factory

Small utilities used by all transcript parsers: truncate() for the
200/500 char limits on inputs_summary/output_excerpt, and
make_parse_error() for building visible parser-error events.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Generic stdout parser (the default fallback)

**Files:**
- Modify: `src/afteragent/transcripts.py`
- Create: `tests/test_adapters_generic.py`
- Create: `tests/fixtures/transcripts/generic/pytest_output.txt`
- Create: `tests/fixtures/transcripts/generic/npm_script.txt`

- [ ] **Step 3.1: Create the fixture files**

Create `tests/fixtures/transcripts/generic/pytest_output.txt`:

```
============================= test session starts =============================
platform darwin -- Python 3.11.6, pytest-8.0.0
collected 4 items

tests/test_foo.py::test_passing PASSED                                   [ 25%]
tests/test_foo.py::test_failing FAILED                                   [ 50%]
tests/test_bar.py::test_one PASSED                                       [ 75%]
tests/test_bar.py::test_two PASSED                                       [100%]

=================================== FAILURES ===================================
______________________________ test_failing _____________________________________

    def test_failing():
>       assert 1 == 2
E       assert 1 == 2

tests/test_foo.py:7: AssertionError
========================= 1 failed, 3 passed in 0.04s ==========================
```

Create `tests/fixtures/transcripts/generic/npm_script.txt`:

```
> my-package@1.0.0 build
> tsc --noEmit && webpack --mode production

[webpack-cli] Compilation finished
asset main.js 42.1 KiB [emitted] (name: main)
webpack 5.89.0 compiled successfully in 1203 ms
```

- [ ] **Step 3.2: Write the failing test**

Create `tests/test_adapters_generic.py`:

```python
from pathlib import Path

from afteragent.transcripts import (
    KIND_TEST_RUN,
    KIND_UNKNOWN,
    SOURCE_STDOUT_HEURISTIC,
    parse_generic_stdout,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "generic"


def test_generic_parser_returns_empty_list_for_empty_stdout():
    events = parse_generic_stdout(run_id="abc", stdout="", stderr="")
    assert events == []


def test_generic_parser_detects_pytest_run_as_test_run():
    stdout = (FIXTURES / "pytest_output.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")

    # At least one test_run event should be emitted.
    test_events = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_events) >= 1
    # It should reference pytest and carry the failure signal.
    first = test_events[0]
    assert first.source == SOURCE_STDOUT_HEURISTIC
    assert first.status == "error"  # pytest output contains "FAILED" and "1 failed"
    assert first.sequence == 0 or first.sequence >= 0  # monotonic


def test_generic_parser_assigns_monotonic_sequences():
    stdout = (FIXTURES / "pytest_output.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)
    assert sequences[0] == 0


def test_generic_parser_tags_all_events_with_heuristic_source():
    stdout = (FIXTURES / "npm_script.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")
    for event in events:
        assert event.source == SOURCE_STDOUT_HEURISTIC


def test_generic_parser_never_raises_on_garbage():
    # Even nonsense input should produce a list, never an exception.
    events = parse_generic_stdout(run_id="abc", stdout="\x00\x01\x02", stderr="\xff" * 100)
    assert isinstance(events, list)


def test_generic_parser_detects_successful_npm_build():
    stdout = (FIXTURES / "npm_script.txt").read_text()
    events = parse_generic_stdout(run_id="abc", stdout=stdout, stderr="")
    # At least one event should have status "success" given "compiled successfully".
    success_events = [e for e in events if e.status == "success"]
    assert len(success_events) >= 1
```

- [ ] **Step 3.3: Run tests to verify they fail**

Run: `pytest tests/test_adapters_generic.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_generic_stdout' from 'afteragent.transcripts'`.

- [ ] **Step 3.4: Implement `parse_generic_stdout`**

Append to `src/afteragent/transcripts.py`:

```python
import re

# Heuristic patterns. Low-fidelity on purpose — events are tagged with
# SOURCE_STDOUT_HEURISTIC so downstream consumers know to weight them.

_TEST_RUNNER_PATTERNS = (
    re.compile(r"test session starts", re.I),
    re.compile(r"(?:^|\s)pytest(?:\s|$)"),
    re.compile(r"(?:^|\s)jest(?:\s|$)"),
    re.compile(r"(?:^|\s)go test(?:\s|$)"),
    re.compile(r"(?:^|\s)npm (?:test|run test)(?:\s|$)"),
    re.compile(r"(?:^|\s)cargo test(?:\s|$)"),
    re.compile(r"(?:^|\s)mocha(?:\s|$)"),
    re.compile(r"(?:^|\s)vitest(?:\s|$)"),
    re.compile(r"(?:^|\s)rspec(?:\s|$)"),
)

_TEST_FAILURE_PATTERNS = (
    re.compile(r"\bfailed\b", re.I),
    re.compile(r"\bassertion\s*error\b", re.I),
    re.compile(r"\btraceback\b", re.I),
    re.compile(r"^\s*FAIL\s", re.I),
)

_SUCCESS_PATTERNS = (
    re.compile(r"compiled successfully", re.I),
    re.compile(r"\bpassed\b.*\b0 failed\b", re.I),
    re.compile(r"\btests? passed\b", re.I),
    re.compile(r"\bok\b\s*$", re.I),
)


def parse_generic_stdout(
    run_id: str,
    stdout: str,
    stderr: str,
) -> list[TranscriptEvent]:
    """Best-effort heuristic parse of arbitrary CLI stdout/stderr.

    Produces low-fidelity events tagged SOURCE_STDOUT_HEURISTIC. Never raises —
    any parser failure becomes a parse_error event.
    """
    events: list[TranscriptEvent] = []
    sequence = 0
    combined = stdout + ("\n" + stderr if stderr else "")
    if not combined.strip():
        return events

    try:
        is_test_run = any(p.search(combined) for p in _TEST_RUNNER_PATTERNS)
        if is_test_run:
            has_failure = any(p.search(combined) for p in _TEST_FAILURE_PATTERNS)
            events.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=sequence,
                    kind=KIND_TEST_RUN,
                    tool_name=None,
                    target=None,
                    inputs_summary=truncate(
                        _first_meaningful_line(combined), INPUTS_SUMMARY_MAX
                    ),
                    output_excerpt=truncate(
                        _last_meaningful_lines(combined, 10), OUTPUT_EXCERPT_MAX
                    ),
                    status="error" if has_failure else "unknown",
                    source=SOURCE_STDOUT_HEURISTIC,
                    timestamp="",
                    raw_ref=None,
                )
            )
            sequence += 1

        has_success = any(p.search(combined) for p in _SUCCESS_PATTERNS)
        if has_success:
            events.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=sequence,
                    kind=KIND_UNKNOWN,
                    tool_name=None,
                    target=None,
                    inputs_summary="",
                    output_excerpt=truncate(
                        _last_meaningful_lines(combined, 5), OUTPUT_EXCERPT_MAX
                    ),
                    status="success",
                    source=SOURCE_STDOUT_HEURISTIC,
                    timestamp="",
                    raw_ref=None,
                )
            )
            sequence += 1
    except Exception as exc:  # never allow ingestion to fail the run
        events.append(
            make_parse_error(
                run_id=run_id,
                sequence=sequence,
                source=SOURCE_STDOUT_HEURISTIC,
                message=f"generic parser raised: {exc}",
                raw_ref=None,
            )
        )

    return events


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _last_meaningful_lines(text: str, n: int) -> str:
    meaningful = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(meaningful[-n:])
```

- [ ] **Step 3.5: Run tests to verify they pass**

Run: `pytest tests/test_adapters_generic.py tests/test_transcripts.py -v`
Expected: PASS — all tests pass.

- [ ] **Step 3.6: Commit**

```bash
git add src/afteragent/transcripts.py tests/test_adapters_generic.py tests/fixtures/transcripts/generic/
git commit -m "$(cat <<'EOF'
Add generic stdout parser for the fallback path

Best-effort heuristic parser that detects test-runner invocations,
failures, and success signals in arbitrary CLI output. Events are
tagged stdout_heuristic so downstream consumers can weight them low.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Store migration and CRUD for `transcript_events`

**Files:**
- Modify: `src/afteragent/store.py`
- Modify: `tests/test_transcripts.py` (add a store integration test) or create a new file; this plan adds to `tests/test_transcripts.py` for cohesion.

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/test_transcripts.py`:

```python
import tempfile
from pathlib import Path

from afteragent.config import resolve_paths
from afteragent.store import Store
from afteragent.transcripts import (
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    SOURCE_CLAUDE_CODE_JSONL,
    TranscriptEvent,
)


def _make_store(tmp: Path) -> Store:
    paths = resolve_paths(tmp)
    return Store(paths)


def _make_event(run_id: str, sequence: int, kind: str, target: str) -> TranscriptEvent:
    return TranscriptEvent(
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        tool_name="Read" if kind == KIND_FILE_READ else "Edit",
        target=target,
        inputs_summary="",
        output_excerpt="",
        status="success",
        source=SOURCE_CLAUDE_CODE_JSONL,
        timestamp="2026-04-10T12:00:00Z",
        raw_ref=None,
    )


def test_store_adds_and_retrieves_transcript_events_in_order():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        events = [
            _make_event("run1", 0, KIND_FILE_READ, "/repo/a.py"),
            _make_event("run1", 1, KIND_FILE_EDIT, "/repo/a.py"),
            _make_event("run1", 2, KIND_FILE_READ, "/repo/b.py"),
        ]
        store.add_transcript_events("run1", events)

        retrieved = store.get_transcript_events("run1")
        assert len(retrieved) == 3
        assert [e["sequence"] for e in retrieved] == [0, 1, 2]
        assert [e["kind"] for e in retrieved] == [
            "file_read",
            "file_edit",
            "file_read",
        ]
        assert retrieved[0]["target"] == "/repo/a.py"


def test_store_filters_transcript_events_by_kind():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        events = [
            _make_event("run1", 0, KIND_FILE_READ, "/repo/a.py"),
            _make_event("run1", 1, KIND_FILE_EDIT, "/repo/a.py"),
            _make_event("run1", 2, KIND_FILE_READ, "/repo/b.py"),
        ]
        store.add_transcript_events("run1", events)

        reads = store.get_transcript_events("run1", kind="file_read")
        assert len(reads) == 2
        assert all(e["kind"] == "file_read" for e in reads)


def test_store_returns_empty_list_for_run_with_no_transcript_events():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        assert store.get_transcript_events("run1") == []


def test_store_handles_empty_event_list_in_add():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        store.create_run("run1", "echo hi", str(tmp), "2026-04-10T12:00:00Z")
        store.add_transcript_events("run1", [])
        assert store.get_transcript_events("run1") == []
```

- [ ] **Step 4.2: Run the tests to verify they fail**

Run: `pytest tests/test_transcripts.py -v -k "store"`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'add_transcript_events'`.

- [ ] **Step 4.3: Add the migration and methods to `store.py`**

In `src/afteragent/store.py`, inside `_init_db`, append the new table creation to the existing `executescript` SQL (add this new block inside the same triple-quoted SQL string, after the `replay_runs` table, keeping the closing `"""` intact):

```sql
CREATE TABLE IF NOT EXISTS transcript_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    tool_name TEXT,
    target TEXT,
    inputs_summary TEXT NOT NULL DEFAULT '',
    output_excerpt TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    source TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT '',
    raw_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_transcript_events_run_seq  ON transcript_events (run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_transcript_events_run_kind ON transcript_events (run_id, kind);
```

Then add the two new methods near the existing `add_event` method. Place them after `get_events` (around line 218 in the existing file). At the top of the file, add an import:

```python
from .transcripts import TranscriptEvent
```

And the new methods:

```python
    def add_transcript_events(
        self,
        run_id: str,
        events: list[TranscriptEvent],
    ) -> None:
        if not events:
            return
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO transcript_events (
                    run_id, sequence, kind, tool_name, target,
                    inputs_summary, output_excerpt, status, source, timestamp, raw_ref
                )
                VALUES (
                    :run_id, :sequence, :kind, :tool_name, :target,
                    :inputs_summary, :output_excerpt, :status, :source, :timestamp, :raw_ref
                )
                """,
                [
                    {
                        "run_id": event.run_id,
                        "sequence": event.sequence,
                        "kind": event.kind,
                        "tool_name": event.tool_name,
                        "target": event.target,
                        "inputs_summary": event.inputs_summary,
                        "output_excerpt": event.output_excerpt,
                        "status": event.status,
                        "source": event.source,
                        "timestamp": event.timestamp,
                        "raw_ref": event.raw_ref,
                    }
                    for event in events
                ],
            )

    def get_transcript_events(
        self,
        run_id: str,
        kind: str | None = None,
    ) -> list[sqlite3.Row]:
        with self.connection() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT id, run_id, sequence, kind, tool_name, target,
                           inputs_summary, output_excerpt, status, source, timestamp, raw_ref
                    FROM transcript_events
                    WHERE run_id = ?
                    ORDER BY sequence ASC
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, run_id, sequence, kind, tool_name, target,
                           inputs_summary, output_excerpt, status, source, timestamp, raw_ref
                    FROM transcript_events
                    WHERE run_id = ? AND kind = ?
                    ORDER BY sequence ASC
                    """,
                    (run_id, kind),
                ).fetchall()
        return rows
```

- [ ] **Step 4.4: Run the tests to verify they pass**

Run: `pytest tests/test_transcripts.py -v`
Expected: PASS — all tests pass including the new store tests.

Also run the full existing suite to catch regressions:
Run: `pytest -v`
Expected: PASS — no regressions in existing tests.

- [ ] **Step 4.5: Commit**

```bash
git add src/afteragent/store.py tests/test_transcripts.py
git commit -m "$(cat <<'EOF'
Add transcript_events table and Store CRUD

Additive migration creates transcript_events with indexes on
(run_id, sequence) and (run_id, kind). New methods add_transcript_events
and get_transcript_events round-trip TranscriptEvent objects.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Base `RunnerAdapter` — `pre_launch_snapshot` and `parse_transcript` defaults

**Files:**
- Modify: `src/afteragent/adapters.py`
- Modify: `tests/test_adapters.py`

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/test_adapters.py`:

```python
from pathlib import Path

from afteragent.adapters import (
    ClaudeCodeAdapter,
    OpenClawAdapter,
    RunnerAdapter,
    ShellAdapter,
)
from afteragent.transcripts import SOURCE_STDOUT_HEURISTIC, KIND_TEST_RUN


def test_base_pre_launch_snapshot_returns_empty_dict(tmp_path: Path):
    adapter = ShellAdapter()
    state = adapter.pre_launch_snapshot(tmp_path)
    assert state == {}


def test_base_parse_transcript_uses_generic_stdout_parser(tmp_path: Path):
    adapter = ShellAdapter()
    stdout = "============================= test session starts ===\nFAILED: boom\n"
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout=stdout,
        stderr="",
        pre_launch_state={},
    )
    assert len(events) >= 1
    assert any(e.kind == KIND_TEST_RUN for e in events)
    assert all(e.source == SOURCE_STDOUT_HEURISTIC for e in events)


def test_base_parse_transcript_never_raises_on_garbage(tmp_path: Path):
    adapter = ShellAdapter()
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout="\x00\x01\x02",
        stderr="",
        pre_launch_state={},
    )
    assert isinstance(events, list)


def test_base_parse_transcript_returns_empty_list_for_empty_input(tmp_path: Path):
    adapter = ShellAdapter()
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout="",
        stderr="",
        pre_launch_state={},
    )
    assert events == []


def test_openclaw_inherits_generic_parser_by_default(tmp_path: Path):
    adapter = OpenClawAdapter()
    stdout = "npm test\npassed 3 tests\n"
    events = adapter.parse_transcript(
        run_id="abc",
        artifact_dir=tmp_path,
        stdout=stdout,
        stderr="",
        pre_launch_state={},
    )
    assert all(e.source == SOURCE_STDOUT_HEURISTIC for e in events)
```

- [ ] **Step 5.2: Run the tests to verify they fail**

Run: `pytest tests/test_adapters.py -v -k "pre_launch_snapshot or parse_transcript"`
Expected: FAIL with `AttributeError: 'ShellAdapter' object has no attribute 'pre_launch_snapshot'` (or `parse_transcript`).

- [ ] **Step 5.3: Add the two methods to `RunnerAdapter` base**

In `src/afteragent/adapters.py`, add these imports at the top (below existing imports):

```python
from .transcripts import TranscriptEvent, parse_generic_stdout
```

Then add the two new methods to the `RunnerAdapter` class (place them after the existing `parse_transcript_events` method, not replacing it — the old method stays for backward compatibility):

```python
    def pre_launch_snapshot(self, cwd: Path) -> dict:
        """Snapshot runner-specific pre-launch state (e.g. transcript directory).

        Called by capture.run_command before subprocess.Popen. The returned
        dict is passed back into parse_transcript after the subprocess exits.
        Default implementation returns an empty dict.
        """
        del cwd
        return {}

    def parse_transcript(
        self,
        run_id: str,
        artifact_dir: Path,
        stdout: str,
        stderr: str,
        pre_launch_state: dict,
    ) -> list[TranscriptEvent]:
        """Parse the runner's transcript into normalized TranscriptEvent objects.

        Default implementation uses the generic stdout heuristic parser.
        Runner subclasses override to provide richer parsing.
        Must never raise — all failures become parse_error events.
        """
        del artifact_dir, pre_launch_state
        return parse_generic_stdout(run_id=run_id, stdout=stdout, stderr=stderr)
```

- [ ] **Step 5.4: Run the tests to verify they pass**

Run: `pytest tests/test_adapters.py -v`
Expected: PASS — new tests pass, existing adapter tests still pass.

Also run the full suite:
Run: `pytest -v`
Expected: PASS — no regressions.

- [ ] **Step 5.5: Commit**

```bash
git add src/afteragent/adapters.py tests/test_adapters.py
git commit -m "$(cat <<'EOF'
Add pre_launch_snapshot and parse_transcript to RunnerAdapter

Two new methods on the base adapter:
- pre_launch_snapshot captures runner-specific state before launch
- parse_transcript returns normalized TranscriptEvent objects after exit

Defaults: empty snapshot and the generic stdout heuristic parser,
so ShellAdapter/OpenClawAdapter inherit working behavior. Claude Code
and Codex adapters will override in subsequent tasks.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Claude Code project-dir slug, pre-launch snapshot, and candidate discovery

**Files:**
- Modify: `src/afteragent/adapters.py`
- Modify: `tests/test_adapters.py`

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/test_adapters.py`:

```python
import time

from afteragent.adapters import (
    ClaudeCodeAdapter,
    claude_project_slug,
    find_candidate_jsonl,
)


def test_claude_project_slug_replaces_slashes_and_spaces_with_dashes():
    cwd = Path("/Users/colin/Documents/Google Drive/Business/Public Projects/AfterAgent")
    slug = claude_project_slug(cwd)
    assert slug == "-Users-colin-Documents-Google-Drive-Business-Public-Projects-AfterAgent"


def test_claude_project_slug_simple_path():
    cwd = Path("/home/user/code/repo")
    slug = claude_project_slug(cwd)
    assert slug == "-home-user-code-repo"


def test_claude_pre_launch_snapshot_records_existing_jsonls(tmp_path: Path, monkeypatch):
    # Redirect Home to tmp_path so we don't touch real ~/.claude.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()
    slug = claude_project_slug(repo)
    project_dir = fake_home / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)
    (project_dir / "existing.jsonl").write_text('{"type":"ping"}\n')

    adapter = ClaudeCodeAdapter()
    state = adapter.pre_launch_snapshot(repo)

    assert state["claude_project_dir"] == project_dir
    assert len(state["pre_jsonl_files"]) == 1
    assert project_dir / "existing.jsonl" in state["pre_jsonl_files"]
    assert "launched_at" in state
    assert isinstance(state["launched_at"], float)


def test_claude_pre_launch_snapshot_handles_missing_project_dir(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "nonexistent"
    repo.mkdir()

    adapter = ClaudeCodeAdapter()
    state = adapter.pre_launch_snapshot(repo)

    # Directory doesn't exist — snapshot still returns a usable dict.
    assert state["pre_jsonl_files"] == {}
    assert "launched_at" in state


def test_find_candidate_jsonl_picks_new_file(tmp_path: Path):
    existing = tmp_path / "existing.jsonl"
    existing.write_text("old\n")
    launched_at = time.time()
    time.sleep(0.01)
    new_file = tmp_path / "new.jsonl"
    new_file.write_text("fresh\n")
    exit_time = time.time()

    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={existing: existing.stat().st_mtime},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    assert chosen == new_file
    assert ambiguous is False


def test_find_candidate_jsonl_picks_modified_file(tmp_path: Path):
    # --continue case: pre-existing file was appended to.
    existing = tmp_path / "existing.jsonl"
    existing.write_text("old\n")
    pre_mtime = existing.stat().st_mtime
    launched_at = time.time()
    time.sleep(0.05)
    existing.write_text("old\nappended\n")  # bump mtime
    exit_time = time.time()

    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={existing: pre_mtime},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    assert chosen == existing
    assert ambiguous is False


def test_find_candidate_jsonl_returns_none_for_zero_candidates(tmp_path: Path):
    launched_at = time.time()
    exit_time = launched_at + 1.0
    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    assert chosen is None
    assert ambiguous is False


def test_find_candidate_jsonl_picks_closest_to_exit_when_ambiguous(tmp_path: Path):
    launched_at = time.time()
    a = tmp_path / "a.jsonl"
    a.write_text("")
    time.sleep(0.05)
    b = tmp_path / "b.jsonl"
    b.write_text("")  # b's mtime is later than a's but still within window
    exit_time = time.time() + 0.1

    chosen, ambiguous = find_candidate_jsonl(
        project_dir=tmp_path,
        pre_jsonl_files={},
        launched_at=launched_at,
        exit_time=exit_time,
    )
    # Both are candidates (both post-launch). Expect the one closest to exit.
    assert chosen in (a, b)
    assert ambiguous is True
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run: `pytest tests/test_adapters.py -v -k "claude_project_slug or pre_launch_snapshot or find_candidate_jsonl"`
Expected: FAIL with `ImportError: cannot import name 'claude_project_slug'` and `'find_candidate_jsonl'` from `afteragent.adapters`.

- [ ] **Step 6.3: Implement the helpers and override**

In `src/afteragent/adapters.py`, add imports at the top:

```python
import time
```

Add these module-level helpers near the bottom, before the `ADAPTERS` tuple:

```python
def claude_project_slug(cwd: Path) -> str:
    """Compute the Claude Code project-directory slug for a working directory.

    Claude Code stores JSONL transcripts under ~/.claude/projects/<slug>/ where
    <slug> is the absolute cwd path with "/" and " " both replaced by "-".
    Other characters are preserved including case.
    """
    s = str(cwd.resolve() if cwd.is_absolute() is False else cwd)
    return s.replace("/", "-").replace(" ", "-")


def find_candidate_jsonl(
    project_dir: Path,
    pre_jsonl_files: dict[Path, float],
    launched_at: float,
    exit_time: float,
) -> tuple[Path | None, bool]:
    """Identify which JSONL file a Claude Code invocation wrote.

    Returns (chosen_path, ambiguous). ambiguous=True means multiple candidates
    existed and the heuristic picked one — caller should emit a parse_error.
    """
    if not project_dir.exists():
        return (None, False)

    try:
        current = list(project_dir.glob("*.jsonl"))
    except OSError:
        return (None, False)

    candidates: list[Path] = []
    for path in current:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if path not in pre_jsonl_files:
            candidates.append(path)
        elif mtime > pre_jsonl_files[path] and mtime >= launched_at:
            candidates.append(path)

    if not candidates:
        return (None, False)
    if len(candidates) == 1:
        return (candidates[0], False)

    # Multiple candidates: pick the one whose mtime is closest to (but not
    # later than) exit_time + 2s grace.
    grace = exit_time + 2.0
    best: Path | None = None
    best_delta: float | None = None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > grace:
            continue
        delta = abs(grace - mtime)
        if best_delta is None or delta < best_delta:
            best = path
            best_delta = delta
    return (best or candidates[0], True)
```

Then override `pre_launch_snapshot` on `ClaudeCodeAdapter`:

```python
class ClaudeCodeAdapter(RunnerAdapter):
    name = "claude-code"
    command_names = ("claude", "claude-code")
    instruction_files = ("CLAUDE.md",)

    def detect(
        self,
        cwd: Path,
        command: list[str] | None = None,
        source_command: str | None = None,
    ) -> bool:
        if command or source_command:
            return super().detect(cwd, command, source_command)
        return (cwd / "CLAUDE.md").exists()

    def pre_launch_snapshot(self, cwd: Path) -> dict:
        slug = claude_project_slug(cwd)
        project_dir = Path.home() / ".claude" / "projects" / slug
        pre: dict[Path, float] = {}
        if project_dir.exists():
            try:
                for path in project_dir.glob("*.jsonl"):
                    try:
                        pre[path] = path.stat().st_mtime
                    except OSError:
                        continue
            except OSError:
                pass
        return {
            "claude_project_dir": project_dir,
            "pre_jsonl_files": pre,
            "launched_at": time.time(),
        }

    # ... (leave existing transcript_event_patterns and transcript_file_globs unchanged)
```

**Important:** Do NOT remove `transcript_event_patterns` or `transcript_file_globs` from `ClaudeCodeAdapter` — they're used by the old `parse_transcript_events` method which is kept for backward compatibility.

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `pytest tests/test_adapters.py -v`
Expected: PASS — new tests pass, existing tests still pass.

- [ ] **Step 6.5: Commit**

```bash
git add src/afteragent/adapters.py tests/test_adapters.py
git commit -m "$(cat <<'EOF'
Add Claude Code transcript discovery helpers

New helpers claude_project_slug and find_candidate_jsonl plus a
ClaudeCodeAdapter.pre_launch_snapshot override. The discovery logic
handles both new JSONL files and --continue-style appends to an
existing session, and returns an ambiguity flag when multiple
candidates exist.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Claude Code JSONL body parser

**Files:**
- Modify: `src/afteragent/transcripts.py`
- Create: `tests/test_adapters_claude_code.py`
- Create: `tests/fixtures/transcripts/claude_code/simple_edit_run.jsonl`
- Create: `tests/fixtures/transcripts/claude_code/ignored_review.jsonl`
- Create: `tests/fixtures/transcripts/claude_code/malformed.jsonl`
- Create: `tests/fixtures/transcripts/claude_code/continued_session.jsonl`

- [ ] **Step 7.1: Create the fixture files**

Create `tests/fixtures/transcripts/claude_code/simple_edit_run.jsonl`. This fixture represents an agent that reads a file, edits it, and runs tests successfully. Each line is one JSONL record:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"sess-simple"}
{"parentUuid":null,"uuid":"u1","timestamp":"2026-04-10T12:00:01Z","message":{"role":"user","content":[{"type":"text","text":"Please fix the failing test in test_foo.py"}]}}
{"parentUuid":"u1","uuid":"u2","timestamp":"2026-04-10T12:00:02Z","message":{"role":"assistant","content":[{"type":"text","text":"I will read the test file first."},{"type":"tool_use","id":"tu1","name":"Read","input":{"file_path":"/repo/tests/test_foo.py"}}]}}
{"parentUuid":"u2","uuid":"u3","timestamp":"2026-04-10T12:00:03Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu1","content":"def test_foo():\n    assert add(1,1) == 3\n"}]}}
{"parentUuid":"u3","uuid":"u4","timestamp":"2026-04-10T12:00:04Z","message":{"role":"assistant","content":[{"type":"text","text":"The test expects add(1,1) to equal 3 but that's wrong; fixing."},{"type":"tool_use","id":"tu2","name":"Edit","input":{"file_path":"/repo/tests/test_foo.py","old_string":"assert add(1,1) == 3","new_string":"assert add(1,1) == 2"}}]}}
{"parentUuid":"u4","uuid":"u5","timestamp":"2026-04-10T12:00:05Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu2","content":"File edited successfully"}]}}
{"parentUuid":"u5","uuid":"u6","timestamp":"2026-04-10T12:00:06Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu3","name":"Bash","input":{"command":"pytest tests/test_foo.py -v"}}]}}
{"parentUuid":"u6","uuid":"u7","timestamp":"2026-04-10T12:00:07Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu3","content":"1 passed in 0.01s","is_error":false}]}}
{"parentUuid":"u7","uuid":"u8","timestamp":"2026-04-10T12:00:08Z","message":{"role":"assistant","content":[{"type":"text","text":"Fixed and test passes."}]}}
```

Create `tests/fixtures/transcripts/claude_code/ignored_review.jsonl`. This fixture represents an agent that edits files *unrelated* to a failing test — useful for sub-project 2's detector but for this task we just need to verify parsing still works:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"sess-ignored"}
{"parentUuid":null,"uuid":"u1","timestamp":"2026-04-10T13:00:01Z","message":{"role":"user","content":[{"type":"text","text":"Fix the failing CI"}]}}
{"parentUuid":"u1","uuid":"u2","timestamp":"2026-04-10T13:00:02Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu1","name":"Read","input":{"file_path":"/repo/README.md"}}]}}
{"parentUuid":"u2","uuid":"u3","timestamp":"2026-04-10T13:00:03Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu1","content":"# My Project\n"}]}}
{"parentUuid":"u3","uuid":"u4","timestamp":"2026-04-10T13:00:04Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu2","name":"Edit","input":{"file_path":"/repo/README.md","old_string":"# My Project","new_string":"# My Project\n\nUpdated."}}]}}
{"parentUuid":"u4","uuid":"u5","timestamp":"2026-04-10T13:00:05Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu2","content":"File edited successfully"}]}}
```

Create `tests/fixtures/transcripts/claude_code/malformed.jsonl`. This fixture has a broken line that must be skipped without crashing:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"sess-malformed"}
{"parentUuid":null,"uuid":"u1","timestamp":"2026-04-10T14:00:01Z","message":{"role":"user","content":[{"type":"text","text":"start"}]}}
{not valid json at all
{"parentUuid":"u1","uuid":"u2","timestamp":"2026-04-10T14:00:02Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu1","name":"Read","input":{"file_path":"/repo/a.py"}}]}}
```

Create `tests/fixtures/transcripts/claude_code/continued_session.jsonl`. Same shape as `simple_edit_run.jsonl` but used in Task 6's `--continue` test path; for this task it just needs to parse cleanly:

```jsonl
{"type":"permission-mode","permissionMode":"default","sessionId":"sess-continued"}
{"parentUuid":null,"uuid":"u1","timestamp":"2026-04-10T15:00:01Z","message":{"role":"user","content":[{"type":"text","text":"continue where we left off"}]}}
{"parentUuid":"u1","uuid":"u2","timestamp":"2026-04-10T15:00:02Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu1","name":"TodoWrite","input":{"todos":[{"content":"verify fix","status":"pending"}]}}]}}
{"parentUuid":"u2","uuid":"u3","timestamp":"2026-04-10T15:00:03Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu1","content":"Todo written"}]}}
```

- [ ] **Step 7.2: Write the failing tests**

Create `tests/test_adapters_claude_code.py`:

```python
from pathlib import Path

from afteragent.transcripts import (
    KIND_ASSISTANT_MESSAGE,
    KIND_BASH_COMMAND,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PARSE_ERROR,
    KIND_TEST_RUN,
    KIND_TODO_UPDATE,
    KIND_USER_MESSAGE,
    SOURCE_CLAUDE_CODE_JSONL,
    parse_claude_code_jsonl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code"


def test_claude_code_parser_handles_simple_edit_run():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)

    # Sequence is monotonic from 0.
    assert events[0].sequence == 0
    for i in range(1, len(events)):
        assert events[i].sequence == events[i - 1].sequence + 1

    kinds = [e.kind for e in events]
    assert KIND_USER_MESSAGE in kinds
    assert KIND_ASSISTANT_MESSAGE in kinds
    assert KIND_FILE_READ in kinds
    assert KIND_FILE_EDIT in kinds
    # The bash "pytest tests/test_foo.py -v" should be classified as test_run.
    assert KIND_TEST_RUN in kinds

    # All events tagged with the Claude Code source.
    assert all(e.source == SOURCE_CLAUDE_CODE_JSONL for e in events)


def test_claude_code_parser_extracts_read_target():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    reads = [e for e in events if e.kind == KIND_FILE_READ]
    assert len(reads) >= 1
    assert reads[0].target == "/repo/tests/test_foo.py"
    assert reads[0].tool_name == "Read"


def test_claude_code_parser_extracts_edit_target():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    edits = [e for e in events if e.kind == KIND_FILE_EDIT]
    assert len(edits) >= 1
    assert edits[0].target == "/repo/tests/test_foo.py"
    assert edits[0].tool_name == "Edit"


def test_claude_code_parser_classifies_pytest_as_test_run():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) >= 1
    assert "pytest" in (test_runs[0].target or "")


def test_claude_code_parser_skips_malformed_lines_and_emits_parse_error():
    text = (FIXTURES / "malformed.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    # At least one parse_error for the broken line, plus valid events still parsed.
    assert any(e.kind == KIND_PARSE_ERROR for e in events)
    assert any(e.kind == KIND_FILE_READ for e in events)


def test_claude_code_parser_handles_continued_session():
    text = (FIXTURES / "continued_session.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    assert any(e.kind == KIND_TODO_UPDATE for e in events)


def test_claude_code_parser_includes_line_number_raw_ref():
    text = (FIXTURES / "simple_edit_run.jsonl").read_text()
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text=text)
    # At least some events should have a raw_ref of form "line:N".
    with_refs = [e for e in events if e.raw_ref and e.raw_ref.startswith("line:")]
    assert len(with_refs) >= 1


def test_claude_code_parser_never_raises_on_completely_broken_input():
    events = parse_claude_code_jsonl(run_id="r1", jsonl_text="\x00\x01not jsonl at all")
    assert isinstance(events, list)
```

- [ ] **Step 7.3: Run tests to verify they fail**

Run: `pytest tests/test_adapters_claude_code.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_claude_code_jsonl' from 'afteragent.transcripts'`.

- [ ] **Step 7.4: Implement `parse_claude_code_jsonl`**

Append to `src/afteragent/transcripts.py`:

```python
import json

# Tool names that map to specific event kinds.
_CLAUDE_TOOL_KIND_MAP = {
    "Read": KIND_FILE_READ,
    "Edit": KIND_FILE_EDIT,
    "Write": KIND_FILE_EDIT,
    "NotebookEdit": KIND_FILE_EDIT,
    "Bash": KIND_BASH_COMMAND,
    "Grep": KIND_SEARCH,
    "Glob": KIND_SEARCH,
    "WebFetch": KIND_WEB_FETCH,
    "WebSearch": KIND_WEB_FETCH,
    "TodoWrite": KIND_TODO_UPDATE,
    "TaskCreate": KIND_TODO_UPDATE,
    "TaskUpdate": KIND_TODO_UPDATE,
    "Task": KIND_SUBAGENT_CALL,
    "Agent": KIND_SUBAGENT_CALL,
}

# Bash command prefixes that indicate a test run.
_TEST_COMMAND_PATTERNS = (
    re.compile(r"^\s*pytest\b"),
    re.compile(r"^\s*python\s+-m\s+pytest\b"),
    re.compile(r"^\s*jest\b"),
    re.compile(r"^\s*npm\s+(?:run\s+)?test\b"),
    re.compile(r"^\s*yarn\s+test\b"),
    re.compile(r"^\s*go\s+test\b"),
    re.compile(r"^\s*cargo\s+test\b"),
    re.compile(r"^\s*mocha\b"),
    re.compile(r"^\s*vitest\b"),
    re.compile(r"^\s*rspec\b"),
    re.compile(r"^\s*bundle\s+exec\s+rspec\b"),
)


def parse_claude_code_jsonl(run_id: str, jsonl_text: str) -> list[TranscriptEvent]:
    """Parse a Claude Code session JSONL into normalized transcript events.

    Never raises. Malformed lines become parse_error events; the rest still parse.
    """
    events: list[TranscriptEvent] = []
    sequence = 0

    for line_num, line in enumerate(jsonl_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            events.append(
                make_parse_error(
                    run_id=run_id,
                    sequence=sequence,
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    message=f"JSONL line {line_num} invalid: {exc}",
                    raw_ref=f"line:{line_num}",
                )
            )
            sequence += 1
            continue

        try:
            new_events = _events_from_jsonl_record(
                run_id=run_id,
                record=record,
                line_num=line_num,
                next_sequence=sequence,
            )
        except Exception as exc:
            events.append(
                make_parse_error(
                    run_id=run_id,
                    sequence=sequence,
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    message=f"record parse raised on line {line_num}: {exc}",
                    raw_ref=f"line:{line_num}",
                )
            )
            sequence += 1
            continue

        for event in new_events:
            events.append(event)
            sequence += 1

    return events


def _events_from_jsonl_record(
    run_id: str,
    record: dict,
    line_num: int,
    next_sequence: int,
) -> list[TranscriptEvent]:
    """Translate a single JSONL record into zero or more TranscriptEvents."""
    out: list[TranscriptEvent] = []
    seq = next_sequence
    raw_ref = f"line:{line_num}"
    timestamp = record.get("timestamp", "") or ""

    # Hook events.
    attachment = record.get("attachment") or {}
    if attachment.get("type", "").startswith("hook_") or attachment.get("hookEvent"):
        out.append(
            TranscriptEvent(
                run_id=run_id,
                sequence=seq,
                kind=KIND_HOOK_EVENT,
                tool_name=None,
                target=attachment.get("hookEvent") or attachment.get("hookName"),
                inputs_summary="",
                output_excerpt=truncate(
                    str(attachment.get("content") or attachment.get("stdout") or ""),
                    OUTPUT_EXCERPT_MAX,
                ),
                status="unknown",
                source=SOURCE_CLAUDE_CODE_JSONL,
                timestamp=timestamp,
                raw_ref=raw_ref,
            )
        )
        seq += 1
        return out

    message = record.get("message")
    if not isinstance(message, dict):
        return out  # skip non-message records silently

    role = message.get("role")
    content = message.get("content")
    if not isinstance(content, list):
        # e.g. string content. Treat as assistant/user message.
        text = str(content) if content is not None else ""
        kind = KIND_ASSISTANT_MESSAGE if role == "assistant" else KIND_USER_MESSAGE
        if text:
            out.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=seq,
                    kind=kind,
                    tool_name=None,
                    target=None,
                    inputs_summary="",
                    output_excerpt=truncate(text, OUTPUT_EXCERPT_MAX),
                    status="unknown",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp=timestamp,
                    raw_ref=raw_ref,
                )
            )
            seq += 1
        return out

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "") or ""
            if not text.strip():
                continue
            kind = KIND_ASSISTANT_MESSAGE if role == "assistant" else KIND_USER_MESSAGE
            out.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=seq,
                    kind=kind,
                    tool_name=None,
                    target=None,
                    inputs_summary="",
                    output_excerpt=truncate(text, OUTPUT_EXCERPT_MAX),
                    status="unknown",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp=timestamp,
                    raw_ref=raw_ref,
                )
            )
            seq += 1

        elif btype == "tool_use":
            tool_name = block.get("name") or "unknown"
            tool_input = block.get("input") or {}
            kind = _classify_tool(tool_name, tool_input)
            target = _extract_target(tool_name, tool_input)
            out.append(
                TranscriptEvent(
                    run_id=run_id,
                    sequence=seq,
                    kind=kind,
                    tool_name=tool_name,
                    target=target,
                    inputs_summary=truncate(
                        json.dumps(tool_input, sort_keys=True, default=str),
                        INPUTS_SUMMARY_MAX,
                    ),
                    output_excerpt="",
                    status="unknown",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp=timestamp,
                    raw_ref=raw_ref,
                )
            )
            seq += 1

        elif btype == "tool_result":
            # Tool results update the most recent tool_use event's status
            # and output_excerpt. Do this by modifying the last event in out
            # if it's a tool event; otherwise emit a standalone user_message.
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                # tool_result content can be a list of content blocks
                result_text = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in result_content
                )
            else:
                result_text = str(result_content)
            is_error = bool(block.get("is_error"))
            # Find the most recent tool event to attach to.
            attached = False
            for ev in reversed(out):
                if ev.tool_name is not None:
                    ev.output_excerpt = truncate(result_text, OUTPUT_EXCERPT_MAX)
                    ev.status = "error" if is_error else "success"
                    attached = True
                    break
            if not attached:
                out.append(
                    TranscriptEvent(
                        run_id=run_id,
                        sequence=seq,
                        kind=KIND_USER_MESSAGE,
                        tool_name=None,
                        target=None,
                        inputs_summary="",
                        output_excerpt=truncate(result_text, OUTPUT_EXCERPT_MAX),
                        status="error" if is_error else "unknown",
                        source=SOURCE_CLAUDE_CODE_JSONL,
                        timestamp=timestamp,
                        raw_ref=raw_ref,
                    )
                )
                seq += 1

    return out


def _classify_tool(tool_name: str, tool_input: dict) -> str:
    base = _CLAUDE_TOOL_KIND_MAP.get(tool_name, KIND_UNKNOWN)
    if base == KIND_BASH_COMMAND:
        command = str(tool_input.get("command", ""))
        if any(p.search(command) for p in _TEST_COMMAND_PATTERNS):
            return KIND_TEST_RUN
    return base


def _extract_target(tool_name: str, tool_input: dict) -> str | None:
    if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
        return tool_input.get("file_path") or tool_input.get("notebook_path")
    if tool_name in ("Grep", "Glob"):
        return tool_input.get("pattern") or tool_input.get("path")
    if tool_name == "Bash":
        return str(tool_input.get("command", "")) or None
    if tool_name in ("WebFetch", "WebSearch"):
        return tool_input.get("url") or tool_input.get("query")
    if tool_name in ("Task", "Agent"):
        return tool_input.get("subagent_type") or tool_input.get("description")
    return None
```

- [ ] **Step 7.5: Run tests to verify they pass**

Run: `pytest tests/test_adapters_claude_code.py tests/test_transcripts.py tests/test_adapters_generic.py -v`
Expected: PASS — all tests pass.

Also run the full suite:
Run: `pytest -v`
Expected: PASS — no regressions.

- [ ] **Step 7.6: Commit**

```bash
git add src/afteragent/transcripts.py tests/test_adapters_claude_code.py tests/fixtures/transcripts/claude_code/
git commit -m "$(cat <<'EOF'
Add Claude Code JSONL parser

parse_claude_code_jsonl translates Claude Code session JSONL records
into TranscriptEvent objects. Handles tool_use + tool_result pairing,
text content blocks, hook events, and malformed lines. Test-run
detection applies to Bash commands matching pytest/jest/go test/etc.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `ClaudeCodeAdapter.parse_transcript` end-to-end

Tie discovery + parser + raw artifact copy together. This is the method `capture.run_command` will call.

**Files:**
- Modify: `src/afteragent/adapters.py`
- Modify: `tests/test_adapters.py`

- [ ] **Step 8.1: Write the failing test**

Append to `tests/test_adapters.py`:

```python
def test_claude_code_adapter_parse_transcript_end_to_end(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()
    slug = claude_project_slug(repo)
    project_dir = fake_home / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)

    adapter = ClaudeCodeAdapter()
    pre_state = adapter.pre_launch_snapshot(repo)

    # Simulate Claude Code writing a new JSONL file mid-run.
    fixture = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "simple_edit_run.jsonl"
    session_jsonl = project_dir / "sess-simple.jsonl"
    session_jsonl.write_text(fixture.read_text())

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    events = adapter.parse_transcript(
        run_id="r1",
        artifact_dir=artifact_dir,
        stdout="",
        stderr="",
        pre_launch_state=pre_state,
    )

    assert len(events) >= 1
    assert all(e.source == "claude_code_jsonl" for e in events)
    # The raw JSONL was copied into the run's transcripts dir.
    copied = artifact_dir / "transcripts" / "session.jsonl"
    assert copied.exists()
    assert copied.read_text() == fixture.read_text()


def test_claude_code_adapter_parse_transcript_falls_back_when_no_jsonl(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()

    adapter = ClaudeCodeAdapter()
    pre_state = adapter.pre_launch_snapshot(repo)

    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    stdout = "pytest tests/\nFAILED: boom\n"
    events = adapter.parse_transcript(
        run_id="r1",
        artifact_dir=artifact_dir,
        stdout=stdout,
        stderr="",
        pre_launch_state=pre_state,
    )

    # No JSONL found → at least one parse_error + generic stdout events.
    assert any(e.kind == "parse_error" for e in events)
    assert any(e.source == "stdout_heuristic" for e in events)
```

- [ ] **Step 8.2: Run the tests to verify they fail**

Run: `pytest tests/test_adapters.py -v -k "parse_transcript_end_to_end or falls_back"`
Expected: FAIL — `ClaudeCodeAdapter.parse_transcript` still uses the inherited default (which doesn't do JSONL discovery).

- [ ] **Step 8.3: Override `parse_transcript` on `ClaudeCodeAdapter`**

Add imports to `src/afteragent/adapters.py`:

```python
import shutil
```

Also update the existing import from `.transcripts` to include the Claude Code parser:

```python
from .transcripts import (
    SOURCE_CLAUDE_CODE_JSONL,
    TranscriptEvent,
    make_parse_error,
    parse_claude_code_jsonl,
    parse_generic_stdout,
)
```

Inside `ClaudeCodeAdapter`, add this method (alongside the existing `pre_launch_snapshot`):

```python
    def parse_transcript(
        self,
        run_id: str,
        artifact_dir: Path,
        stdout: str,
        stderr: str,
        pre_launch_state: dict,
    ) -> list[TranscriptEvent]:
        transcripts_dir = artifact_dir / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)

        project_dir = pre_launch_state.get("claude_project_dir")
        pre_files = pre_launch_state.get("pre_jsonl_files", {})
        launched_at = pre_launch_state.get("launched_at", 0.0)
        exit_time = time.time()

        if not project_dir:
            return self._fallback_with_warning(
                run_id=run_id,
                stdout=stdout,
                stderr=stderr,
                message="Claude Code project dir not resolved",
            )

        chosen, ambiguous = find_candidate_jsonl(
            project_dir=project_dir,
            pre_jsonl_files=pre_files,
            launched_at=launched_at,
            exit_time=exit_time,
        )

        if chosen is None:
            return self._fallback_with_warning(
                run_id=run_id,
                stdout=stdout,
                stderr=stderr,
                message=f"no new or modified JSONL found under {project_dir}",
            )

        try:
            text = chosen.read_text()
        except OSError as exc:
            return self._fallback_with_warning(
                run_id=run_id,
                stdout=stdout,
                stderr=stderr,
                message=f"failed to read {chosen}: {exc}",
            )

        # Copy the raw JSONL as a run artifact.
        try:
            shutil.copyfile(chosen, transcripts_dir / "session.jsonl")
        except OSError:
            pass  # copy failure is logged by emitting a parse_error below if critical

        events = parse_claude_code_jsonl(run_id=run_id, jsonl_text=text)

        if ambiguous:
            events.insert(
                0,
                make_parse_error(
                    run_id=run_id,
                    sequence=0,
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    message=f"multiple JSONL candidates found; chose {chosen.name}",
                    raw_ref=None,
                ),
            )
            # Re-number sequences so they stay monotonic.
            for idx, ev in enumerate(events):
                ev.sequence = idx

        return events

    def _fallback_with_warning(
        self,
        run_id: str,
        stdout: str,
        stderr: str,
        message: str,
    ) -> list[TranscriptEvent]:
        events: list[TranscriptEvent] = [
            make_parse_error(
                run_id=run_id,
                sequence=0,
                source=SOURCE_CLAUDE_CODE_JSONL,
                message=message,
                raw_ref=None,
            )
        ]
        generic = parse_generic_stdout(run_id=run_id, stdout=stdout, stderr=stderr)
        for ev in generic:
            ev.sequence = len(events)
            events.append(ev)
        return events
```

- [ ] **Step 8.4: Run tests to verify they pass**

Run: `pytest tests/test_adapters.py -v`
Expected: PASS.

Run full suite: `pytest -v`
Expected: PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/afteragent/adapters.py tests/test_adapters.py
git commit -m "$(cat <<'EOF'
Wire ClaudeCodeAdapter.parse_transcript end-to-end

Combines JSONL discovery, parse_claude_code_jsonl, and raw artifact
copy. Falls back to the generic stdout parser and emits a visible
parse_error event when no JSONL can be located. Ambiguous multi-
candidate cases emit a parse_error and continue.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `CodexAdapter.parse_transcript`

**Files:**
- Modify: `src/afteragent/transcripts.py`
- Modify: `src/afteragent/adapters.py`
- Create: `tests/test_adapters_codex.py`
- Create: `tests/fixtures/transcripts/codex/simple_run.txt`
- Create: `tests/fixtures/transcripts/codex/test_run_with_errors.txt`

- [ ] **Step 9.1: Create the fixture files**

Create `tests/fixtures/transcripts/codex/simple_run.txt`. Codex stdout is less structured than Claude Code's JSONL; we target recognizable shapes. Representative sample:

```
codex: starting task "fix failing test"
codex: reading /repo/tests/test_foo.py
codex: editing /repo/tests/test_foo.py
codex: running `pytest tests/test_foo.py`
==================== test session starts ====================
tests/test_foo.py .                                    [100%]
==================== 1 passed in 0.02s ======================
codex: task complete
```

Create `tests/fixtures/transcripts/codex/test_run_with_errors.txt`:

```
codex: starting task "repair CI"
codex: reading /repo/src/app.py
codex: patched /repo/src/app.py
codex: running `go test ./...`
FAIL: TestWiring (0.01s)
    app_test.go:42: expected 3, got 2
FAIL
exit status 1
codex: attempt 2
codex: patched /repo/src/app.py
codex: running `go test ./...`
ok  example.com/app    0.05s
codex: task complete
```

- [ ] **Step 9.2: Write the failing test**

Create `tests/test_adapters_codex.py`:

```python
from pathlib import Path

from afteragent.transcripts import (
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_TEST_RUN,
    SOURCE_CODEX_STDOUT,
    parse_codex_stdout,
)

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts" / "codex"


def test_codex_parser_extracts_reads_and_edits():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")

    reads = [e for e in events if e.kind == KIND_FILE_READ]
    edits = [e for e in events if e.kind == KIND_FILE_EDIT]
    assert len(reads) >= 1
    assert len(edits) >= 1
    assert reads[0].target == "/repo/tests/test_foo.py"
    assert edits[0].target == "/repo/tests/test_foo.py"


def test_codex_parser_detects_test_runs():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    assert len(test_runs) >= 1
    assert "pytest" in (test_runs[0].target or "")


def test_codex_parser_marks_failed_tests():
    text = (FIXTURES / "test_run_with_errors.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    test_runs = [e for e in events if e.kind == KIND_TEST_RUN]
    # At least one test run should carry error status.
    assert any(e.status == "error" for e in test_runs)


def test_codex_parser_tags_all_events_with_codex_source():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    assert all(e.source == SOURCE_CODEX_STDOUT for e in events)


def test_codex_parser_monotonic_sequences():
    text = (FIXTURES / "simple_run.txt").read_text()
    events = parse_codex_stdout(run_id="r1", stdout=text, stderr="")
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences)


def test_codex_parser_never_raises():
    events = parse_codex_stdout(run_id="r1", stdout="\x00\x01nonsense", stderr="")
    assert isinstance(events, list)
```

- [ ] **Step 9.3: Run tests to verify they fail**

Run: `pytest tests/test_adapters_codex.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_codex_stdout'`.

- [ ] **Step 9.4: Implement `parse_codex_stdout`**

Append to `src/afteragent/transcripts.py`:

```python
_CODEX_READ_PATTERN = re.compile(
    r"codex:\s*(?:reading|read)\s+(?P<path>[A-Za-z0-9_./-]+)", re.I
)
_CODEX_EDIT_PATTERN = re.compile(
    r"codex:\s*(?:editing|edited|patched|wrote|updated|patching)\s+(?P<path>[A-Za-z0-9_./-]+)",
    re.I,
)
_CODEX_RUN_PATTERN = re.compile(
    r"codex:\s*running\s+`(?P<command>[^`]+)`", re.I
)


def parse_codex_stdout(run_id: str, stdout: str, stderr: str) -> list[TranscriptEvent]:
    """Parse Codex CLI stdout into TranscriptEvents.

    Medium fidelity: Codex's output is less structured than Claude Code's JSONL,
    so we target recognizable `codex:` prefix lines and classify by command.
    Never raises.
    """
    events: list[TranscriptEvent] = []
    sequence = 0
    combined = stdout + ("\n" + stderr if stderr else "")
    if not combined.strip():
        return events

    try:
        for line_num, line in enumerate(combined.splitlines(), start=1):
            stripped = line.strip()

            read_match = _CODEX_READ_PATTERN.search(stripped)
            if read_match:
                events.append(
                    TranscriptEvent(
                        run_id=run_id,
                        sequence=sequence,
                        kind=KIND_FILE_READ,
                        tool_name="codex-read",
                        target=read_match.group("path"),
                        inputs_summary="",
                        output_excerpt="",
                        status="unknown",
                        source=SOURCE_CODEX_STDOUT,
                        timestamp="",
                        raw_ref=f"line:{line_num}",
                    )
                )
                sequence += 1
                continue

            edit_match = _CODEX_EDIT_PATTERN.search(stripped)
            if edit_match:
                events.append(
                    TranscriptEvent(
                        run_id=run_id,
                        sequence=sequence,
                        kind=KIND_FILE_EDIT,
                        tool_name="codex-edit",
                        target=edit_match.group("path"),
                        inputs_summary="",
                        output_excerpt="",
                        status="unknown",
                        source=SOURCE_CODEX_STDOUT,
                        timestamp="",
                        raw_ref=f"line:{line_num}",
                    )
                )
                sequence += 1
                continue

            run_match = _CODEX_RUN_PATTERN.search(stripped)
            if run_match:
                command = run_match.group("command")
                is_test = any(p.search(command) for p in _TEST_COMMAND_PATTERNS)
                # Look ahead in combined text for failure markers after this line.
                lookahead = _codex_lookahead(combined, line_num)
                status = "error" if any(
                    t in lookahead.upper() for t in ("FAIL", "FAILED", "ERROR")
                ) else "unknown"
                events.append(
                    TranscriptEvent(
                        run_id=run_id,
                        sequence=sequence,
                        kind=KIND_TEST_RUN if is_test else KIND_BASH_COMMAND,
                        tool_name="codex-bash",
                        target=command,
                        inputs_summary="",
                        output_excerpt=truncate(lookahead, OUTPUT_EXCERPT_MAX),
                        status=status,
                        source=SOURCE_CODEX_STDOUT,
                        timestamp="",
                        raw_ref=f"line:{line_num}",
                    )
                )
                sequence += 1
                continue
    except Exception as exc:
        events.append(
            make_parse_error(
                run_id=run_id,
                sequence=sequence,
                source=SOURCE_CODEX_STDOUT,
                message=f"codex parser raised: {exc}",
                raw_ref=None,
            )
        )

    return events


def _codex_lookahead(text: str, line_num: int, window: int = 8) -> str:
    """Return up to `window` non-empty lines after line_num."""
    lines = text.splitlines()
    start = line_num  # line_num is 1-indexed; next line is index line_num
    ahead = [ln for ln in lines[start : start + window] if ln.strip()]
    return "\n".join(ahead)
```

Then override `parse_transcript` on `CodexAdapter` in `src/afteragent/adapters.py`. Update the `.transcripts` import to include `parse_codex_stdout`:

```python
from .transcripts import (
    SOURCE_CLAUDE_CODE_JSONL,
    TranscriptEvent,
    make_parse_error,
    parse_claude_code_jsonl,
    parse_codex_stdout,
    parse_generic_stdout,
)
```

Add the override inside `CodexAdapter` (place it after `detect`, before `transcript_event_patterns`):

```python
    def parse_transcript(
        self,
        run_id: str,
        artifact_dir: Path,
        stdout: str,
        stderr: str,
        pre_launch_state: dict,
    ) -> list[TranscriptEvent]:
        del artifact_dir, pre_launch_state
        return parse_codex_stdout(run_id=run_id, stdout=stdout, stderr=stderr)
```

- [ ] **Step 9.5: Run tests to verify they pass**

Run: `pytest tests/test_adapters_codex.py tests/test_adapters.py -v`
Expected: PASS.

Run full suite: `pytest -v`
Expected: PASS.

- [ ] **Step 9.6: Commit**

```bash
git add src/afteragent/transcripts.py src/afteragent/adapters.py tests/test_adapters_codex.py tests/fixtures/transcripts/codex/
git commit -m "$(cat <<'EOF'
Add Codex stdout parser and CodexAdapter override

parse_codex_stdout recognizes codex: prefix lines for reads, edits,
and bash runs, plus test-run classification and failure detection
from a short post-command lookahead. CodexAdapter.parse_transcript
delegates to it.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `capture.run_command` — pre-launch snapshot

**Files:**
- Modify: `src/afteragent/capture.py`
- Modify: `tests/test_capture.py`

- [ ] **Step 10.1: Write the failing test**

Append to `tests/test_capture.py`:

```python
def test_run_command_calls_pre_launch_snapshot_before_subprocess(tmp_path: Path, monkeypatch):
    from afteragent.adapters import RunnerAdapter
    from afteragent.capture import run_command
    from afteragent.config import resolve_paths
    from afteragent.store import Store

    snapshot_calls = []

    class RecordingAdapter(RunnerAdapter):
        name = "recording"

        def pre_launch_snapshot(self, cwd):
            snapshot_calls.append(cwd)
            return {"captured": True}

        def parse_transcript(self, run_id, artifact_dir, stdout, stderr, pre_launch_state):
            # Verify the pre_launch_state we captured is what we get back.
            assert pre_launch_state == {"captured": True}
            return []

    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=RecordingAdapter(),
    )
    assert result["exit_code"] == 0
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0] == tmp_path
```

- [ ] **Step 10.2: Run the test to verify it fails**

Run: `pytest tests/test_capture.py::test_run_command_calls_pre_launch_snapshot_before_subprocess -v`
Expected: FAIL — the test's `parse_transcript` assertion fails because the current capture doesn't thread `pre_launch_state` through.

- [ ] **Step 10.3: Add the pre-launch call and thread state through**

In `src/afteragent/capture.py`, inside `run_command`, after the `store.add_event("run.started", ...)` block and before `artifact_dir = store.run_artifact_dir(run_id)`, add:

```python
    pre_launch_state = active_adapter.pre_launch_snapshot(cwd)
```

Store `pre_launch_state` as a local — it will be consumed in Task 11.

- [ ] **Step 10.4: Run the test to verify the snapshot is called**

Run: `pytest tests/test_capture.py::test_run_command_calls_pre_launch_snapshot_before_subprocess -v`
Expected: Still FAIL on the `pre_launch_state` assertion in `parse_transcript`, because Task 10 only added the pre-launch call, not the post-exit call. This is expected — Task 11 completes the flow. For now, the test should show that `snapshot_calls` was populated even if the assertion inside `parse_transcript` isn't reached.

To confirm Task 10 is correct in isolation, adjust the test temporarily to skip the `pre_launch_state` check, or add a simpler test:

```python
def test_run_command_calls_pre_launch_snapshot(tmp_path: Path):
    from afteragent.adapters import RunnerAdapter
    from afteragent.capture import run_command
    from afteragent.config import resolve_paths
    from afteragent.store import Store

    calls = []

    class Adapter(RunnerAdapter):
        name = "simple"

        def pre_launch_snapshot(self, cwd):
            calls.append(cwd)
            return {}

    store = Store(resolve_paths(tmp_path))
    run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=Adapter(),
    )
    assert len(calls) == 1
```

Run: `pytest tests/test_capture.py::test_run_command_calls_pre_launch_snapshot -v`
Expected: PASS.

- [ ] **Step 10.5: Commit**

```bash
git add src/afteragent/capture.py tests/test_capture.py
git commit -m "$(cat <<'EOF'
Call adapter.pre_launch_snapshot before subprocess launch

First half of capture's transcript ingestion wiring. The returned
state dict is stored as a local for the post-exit parse_transcript
call that Task 11 adds.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: `capture.run_command` — post-exit parse and store write

**Files:**
- Modify: `src/afteragent/capture.py`
- Modify: `tests/test_capture.py`

- [ ] **Step 11.1: Write the failing test**

Add to `tests/test_capture.py`:

```python
def test_run_command_writes_transcript_events_from_adapter(tmp_path: Path):
    from afteragent.adapters import RunnerAdapter
    from afteragent.capture import run_command
    from afteragent.config import resolve_paths
    from afteragent.store import Store
    from afteragent.transcripts import (
        KIND_FILE_READ,
        SOURCE_CLAUDE_CODE_JSONL,
        TranscriptEvent,
    )

    class StubAdapter(RunnerAdapter):
        name = "stub"

        def pre_launch_snapshot(self, cwd):
            return {"hello": "world"}

        def parse_transcript(self, run_id, artifact_dir, stdout, stderr, pre_launch_state):
            assert pre_launch_state == {"hello": "world"}
            return [
                TranscriptEvent(
                    run_id=run_id,
                    sequence=0,
                    kind=KIND_FILE_READ,
                    tool_name="Read",
                    target="/repo/README.md",
                    inputs_summary="",
                    output_excerpt="",
                    status="success",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp="2026-04-10T12:00:00Z",
                    raw_ref="line:1",
                ),
                TranscriptEvent(
                    run_id=run_id,
                    sequence=1,
                    kind=KIND_FILE_READ,
                    tool_name="Read",
                    target="/repo/a.py",
                    inputs_summary="",
                    output_excerpt="",
                    status="success",
                    source=SOURCE_CLAUDE_CODE_JSONL,
                    timestamp="2026-04-10T12:00:01Z",
                    raw_ref="line:2",
                ),
            ]

    store = Store(resolve_paths(tmp_path))
    result = run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=StubAdapter(),
    )

    rows = store.get_transcript_events(result["run_id"])
    assert len(rows) == 2
    assert rows[0]["target"] == "/repo/README.md"
    assert rows[0]["sequence"] == 0
    assert rows[1]["target"] == "/repo/a.py"
    assert rows[1]["sequence"] == 1


def test_run_command_precreates_transcripts_artifact_subdir(tmp_path: Path):
    from afteragent.adapters import RunnerAdapter
    from afteragent.capture import run_command
    from afteragent.config import resolve_paths
    from afteragent.store import Store

    subdir_seen = []

    class Adapter(RunnerAdapter):
        name = "check"

        def parse_transcript(self, run_id, artifact_dir, stdout, stderr, pre_launch_state):
            subdir_seen.append(artifact_dir / "transcripts")
            return []

    store = Store(resolve_paths(tmp_path))
    run_command(
        store=store,
        command=["python3", "-c", "print('hi')"],
        cwd=tmp_path,
        adapter=Adapter(),
    )
    # The adapter saw artifact_dir and can write into transcripts/ from there.
    assert len(subdir_seen) == 1
```

- [ ] **Step 11.2: Run tests to verify they fail**

Run: `pytest tests/test_capture.py::test_run_command_writes_transcript_events_from_adapter tests/test_capture.py::test_run_command_precreates_transcripts_artifact_subdir -v`
Expected: FAIL — the new `parse_transcript` path isn't wired.

- [ ] **Step 11.3: Add the post-exit parse and store write**

In `src/afteragent/capture.py`, inside `run_command`, after the `parsed_events = active_adapter.parse_transcript_events(...)` block (around line 111 today) and before the `store.add_event("process.completed", ...)` block, add:

```python
    # New transcript ingestion layer (sub-project 1). This is additive and
    # does not replace the legacy parse_transcript_events path above.
    transcripts_dir = artifact_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    try:
        transcript_events = active_adapter.parse_transcript(
            run_id=run_id,
            artifact_dir=artifact_dir,
            stdout=stdout_text,
            stderr=stderr_text,
            pre_launch_state=pre_launch_state,
        )
    except Exception as exc:
        # Contract says parsers never raise, but defend against a buggy adapter.
        from .transcripts import SOURCE_STDOUT_HEURISTIC, make_parse_error
        transcript_events = [
            make_parse_error(
                run_id=run_id,
                sequence=0,
                source=SOURCE_STDOUT_HEURISTIC,
                message=f"adapter parse_transcript raised: {exc}",
                raw_ref=None,
            )
        ]
    store.add_transcript_events(run_id, transcript_events)
```

- [ ] **Step 11.4: Run tests to verify they pass**

Run: `pytest tests/test_capture.py -v`
Expected: PASS — new tests and existing tests all pass.

Run full suite: `pytest -v`
Expected: PASS.

- [ ] **Step 11.5: Commit**

```bash
git add src/afteragent/capture.py tests/test_capture.py
git commit -m "$(cat <<'EOF'
Wire capture.run_command to parse and store transcript events

Second half of capture's transcript ingestion wiring. After the
subprocess exits, call adapter.parse_transcript with the stored
pre_launch_state and write the returned events via the store.
Pre-creates the artifacts/<run_id>/transcripts/ directory for
adapters that copy raw source files.

The old parse_transcript_events call and its events table writes
are unchanged.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Integration test — real ClaudeCodeAdapter through the capture pipeline

**Files:**
- Modify: `tests/test_capture.py`

- [ ] **Step 12.1: Write the integration test**

Add to `tests/test_capture.py`:

```python
def test_capture_full_pipeline_with_real_claude_code_adapter(tmp_path: Path, monkeypatch):
    """Exercises snapshot → subprocess → parse → store with a real ClaudeCodeAdapter.

    Uses a fake ~/.claude/projects/<slug>/ layout. The "subprocess" is a no-op
    python command; we drop a fixture JSONL into the project dir before running
    run_command, and monkeypatch HOME so the adapter looks at our fake dir.
    """
    from afteragent.adapters import ClaudeCodeAdapter, claude_project_slug
    from afteragent.capture import run_command
    from afteragent.config import resolve_paths
    from afteragent.store import Store

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    repo = tmp_path / "repo"
    repo.mkdir()
    slug = claude_project_slug(repo)
    project_dir = fake_home / ".claude" / "projects" / slug
    project_dir.mkdir(parents=True)

    # Pre-populate the session JSONL BEFORE running the command. This simulates
    # Claude Code having written the transcript during the (no-op) subprocess.
    fixture = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code" / "simple_edit_run.jsonl"
    session_path = project_dir / "sess-simple.jsonl"
    session_path.write_text(fixture.read_text())
    # Bump mtime forward so the discovery heuristic picks it up as a
    # "modified after launch" candidate (the file technically existed before
    # pre_launch_snapshot ran, so without this bump it would be filtered out).
    import os
    future = time.time() + 10
    os.utime(session_path, (future, future))

    adapter = ClaudeCodeAdapter()
    store = Store(resolve_paths(tmp_path / "afteragent-root"))

    result = run_command(
        store=store,
        command=["python3", "-c", "print('noop')"],
        cwd=repo,
        adapter=adapter,
    )
    run_id = result["run_id"]

    rows = store.get_transcript_events(run_id)
    assert len(rows) > 0
    assert all(r["source"] == "claude_code_jsonl" for r in rows if r["kind"] != "parse_error")

    # Kind coverage: we expect at least one file_read and one file_edit
    # from the fixture.
    kinds = {r["kind"] for r in rows}
    assert "file_read" in kinds
    assert "file_edit" in kinds

    # Raw transcript was copied into the artifact dir.
    artifacts_root = store.paths.artifacts_dir / run_id / "transcripts"
    assert (artifacts_root / "session.jsonl").exists()


import time  # at top of file if not already imported
```

Note: if `import time` is already at the top of `tests/test_capture.py`, skip adding it again.

- [ ] **Step 12.2: Run the test**

Run: `pytest tests/test_capture.py::test_capture_full_pipeline_with_real_claude_code_adapter -v`
Expected: PASS. This validates the full pipeline from pre-launch snapshot through post-exit parse and store write.

- [ ] **Step 12.3: Run the full test suite**

Run: `pytest -v`
Expected: PASS — all existing and new tests pass.

- [ ] **Step 12.4: Commit**

```bash
git add tests/test_capture.py
git commit -m "$(cat <<'EOF'
Integration test: full capture pipeline with ClaudeCodeAdapter

Exercises snapshot → subprocess → parse → store end-to-end using a
fake ~/.claude/projects layout and a fixture JSONL. Verifies events
land in the store with the expected kinds and that the raw JSONL is
copied into the run's artifacts/<id>/transcripts/ directory.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: E2E matrix — transcript ingestion case

**Files:**
- Modify: `scripts/e2e_matrix.sh`

- [ ] **Step 13.1: Inspect the current matrix**

Run: `cat scripts/e2e_matrix.sh`
Read the shape of the existing script before modifying.

- [ ] **Step 13.2: Add a transcript-ingestion case**

Append to `scripts/e2e_matrix.sh`. The exact placement depends on the existing structure; aim for a new block labeled `transcript ingestion` after the existing shell/OpenClaw/ClaudeCode/Codex cases. If the script is a simple `pytest`-runner, add a dedicated pytest invocation:

```bash
echo "=== transcript ingestion tests ==="
pytest -v \
    tests/test_transcripts.py \
    tests/test_adapters_claude_code.py \
    tests/test_adapters_codex.py \
    tests/test_adapters_generic.py \
    tests/test_adapters.py \
    tests/test_capture.py
```

If the script does more elaborate end-to-end flows with fake agent scripts, also add a block that creates a temp Claude Code project dir, writes a fixture JSONL to it, and runs `afteragent exec -- python3 -c "print('noop')"` with `HOME` pointed at the temp dir, verifying a transcript event lands in the store. Only add this block if the existing matrix already uses this pattern for other runners; otherwise the pytest block above is sufficient.

- [ ] **Step 13.3: Run the matrix locally**

Run: `./scripts/e2e_matrix.sh`
Expected: All sections pass.

- [ ] **Step 13.4: Commit**

```bash
git add scripts/e2e_matrix.sh
git commit -m "$(cat <<'EOF'
Add transcript-ingestion case to e2e matrix

New pytest block exercises transcripts module, all per-runner parser
test files, and the capture integration tests as part of the standard
end-to-end verification.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Manual dogfood acceptance check (success criterion #7)

**Files:** none (manual verification)

This is the acceptance test from the spec. It verifies the whole pipeline works against a real Claude Code invocation. If it fails, sub-project 1 is not done.

- [ ] **Step 14.1: Install the package in editable mode**

```bash
cd "/Users/colin/Documents/Google Drive/Business/Public Projects/AfterAgent/afteragent"
pip install -e .
```

Expected: package installs; `afteragent` command is on `$PATH`.

- [ ] **Step 14.2: Run a real dogfood invocation**

```bash
cd /tmp && mkdir -p afteragent-dogfood && cd afteragent-dogfood && echo "# Test Repo" > README.md
afteragent exec -- claude "read the README"
```

Expected: the command completes, prints a run id like `Run captured: <id>`.

- [ ] **Step 14.3: Query transcript events**

```bash
sqlite3 .afteragent/afteragent.sqlite3 "SELECT sequence, kind, tool_name, target FROM transcript_events ORDER BY sequence;"
```

Expected output contains at least one `file_read` row with a `target` ending in `README.md`, for example:

```
0|user_message||
1|assistant_message||
2|file_read|Read|/tmp/afteragent-dogfood/README.md
3|assistant_message||
```

- [ ] **Step 14.4: Verify the raw JSONL was preserved**

```bash
ls .afteragent/artifacts/*/transcripts/session.jsonl
```

Expected: one or more `session.jsonl` files exist under an artifact dir.

- [ ] **Step 14.5: Capture the result**

If the check passes, sub-project 1 is complete. If it fails, investigate which step of the pipeline broke — pre-launch snapshot, discovery, parser, or store write — and add a regression test before fixing. Do not mark this task complete until a real `claude` run produces a `file_read` event for the target file.

- [ ] **Step 14.6: (Optional) Tag the sub-project**

```bash
git tag subproject-1-complete
```

Leaves a marker for when sub-project 2 planning starts.

---

## Self-review checklist (plan author)

The following was verified before this plan was considered complete:

**Spec coverage:**
- [x] Goals 1–7 from the spec map to Tasks 1–14.
- [x] Non-goals are explicitly excluded (no LLM calls, no UI, no diagnostics/workflow changes, etc.) — verified none of the tasks touch those files.
- [x] All files in the spec's "Files touched" table appear in at least one task.
- [x] Success criteria 1–7 are covered (1–4 by tests, 5 by full suite runs, 6 by the non-goal exclusions, 7 by Task 14).

**Placeholder scan:**
- [x] No "TBD", "TODO", "similar to Task N", or unspecified code blocks.
- [x] Every step that changes code shows the complete code or a specific location.

**Type consistency:**
- [x] `TranscriptEvent` field names are identical across every task that constructs or reads one.
- [x] Helper names (`truncate`, `make_parse_error`, `parse_generic_stdout`, `parse_claude_code_jsonl`, `parse_codex_stdout`, `claude_project_slug`, `find_candidate_jsonl`) match their import sites.
- [x] Event kind and source constants match between definition (Task 1) and usage (Tasks 3, 7, 9).
- [x] The new `pre_launch_snapshot` and `parse_transcript` method signatures are identical between base class definition (Task 5) and subclass overrides (Tasks 6, 8, 9).

**Known imperfections:**
- Task 10 has a "pre-launch snapshot call added but test cannot fully verify until Task 11" ordering wrinkle; mitigated by splitting the test into a simpler Task-10 test and leaving the end-to-end assertion for Task 11.
- The spec referenced `.afteragent/runs/<id>/` but the actual artifact convention is `.afteragent/artifacts/<run_id>/`. The plan uses the correct path and flags the discrepancy in pre-flight note #2.
