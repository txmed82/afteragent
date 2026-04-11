# LLM-Driven Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an LLM layer that reviews and augments the 6 rule-based detectors in `diagnostics.py` and replaces the hardcoded intervention strings with LLM-authored text. Supports 4 providers (Anthropic, OpenAI, OpenRouter, Ollama) via two API shapes. Graceful degradation to rule-based behavior when no provider is configured.

**Architecture:** New `src/afteragent/llm/` package owns config loading, provider client adapters, structured-output schemas, prompt composition, and the `enhance_diagnosis_with_llm` orchestration. Existing `diagnostics.py` and `store.py` gain additive hooks. LLM invocation is decoupled from read paths — the UI and existing CLI subcommands stay rule-based-and-fast; LLM runs only via explicit `afteragent enhance` command or the `auto_enhance_on_exec` config flag.

**Tech Stack:** Python 3.11+, stdlib `tomllib` for config, `anthropic` and `openai` SDKs as optional dependencies, `jsonschema` as a dev dependency for schema validation tests. All existing sub-project 1 infrastructure (transcript_events, models, store patterns).

---

## Reference documents

- **Spec:** `docs/superpowers/specs/2026-04-10-llm-diagnosis-design.md` — read first for goals/non-goals/success criteria.
- **Sub-project 1 plan:** `docs/superpowers/plans/2026-04-10-transcript-ingestion.md` — prior sub-project, sets the codebase conventions this plan follows.

## Pre-flight notes

1. **Branch:** this plan was written against `afteragent-subproject-2` branched from the tip of `afteragent-subproject-1` (at commit `d8a2d3b` as of 2026-04-10). Do not push/pull/switch branches mid-execution. Commit locally only.
2. **Existing test count:** 91/91 pytest + 28 unittest + 2 e2e. Every task's final step must preserve "all existing tests pass" — add-only, never remove or modify existing tests except where this plan explicitly says to.
3. **Sub-project 1 is not yet merged to master.** It lives in PR #5 on the `afteragent-subproject-1` branch, which is this plan's base. If sub-project 1 merges to master during execution, the sub-project 2 branch can be rebased onto master cleanly — no code conflicts expected since sub-project 2 only extends (never modifies) sub-project 1's files.
4. **Optional dependencies:** the `anthropic` and `openai` SDKs are added as optional extras. Tests must NOT require either SDK to be installed by default — they should mock the SDK imports at the module level. An engineer running `pytest` on a clean checkout with only stdlib should still get a green suite minus the gated live tests.
5. **`tomllib` availability:** Python 3.11+ has `tomllib` in stdlib. `pyproject.toml` already requires `python>=3.11`. No new runtime dep.
6. **API keys in tests:** no test should ever require a real API key. Integration tests that do are gated behind `AFTERAGENT_LLM_LIVE_TEST=1` and skipped otherwise.
7. **Commit style:** follow existing commits, imperative mood, one-line subject. Include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>` footer on every commit.
8. **Test runner:** `python3 -m pytest` from the repo root. Individual test: `python3 -m pytest tests/test_name.py::test_func -v`.

## File structure

**New files (create in this order):**

```
src/afteragent/llm/__init__.py                    # Package init + public exports
src/afteragent/llm/config.py                      # LLMConfig dataclass + load_config()
src/afteragent/llm/cost_table.py                  # Per-model pricing map
src/afteragent/llm/client.py                      # LLMClient Protocol + StructuredResponse + get_client()
src/afteragent/llm/anthropic_client.py            # AnthropicClient (lazy imports anthropic SDK)
src/afteragent/llm/openai_client.py               # OpenAICompatClient (lazy imports openai SDK)
src/afteragent/llm/schemas.py                     # FINDINGS_SCHEMA + INTERVENTIONS_SCHEMA
src/afteragent/llm/prompts.py                     # DiagnosisContext dataclass + prompt builders
src/afteragent/llm/merge.py                       # Pure merge logic for confirm/reject/novel
src/afteragent/llm/enhancer.py                    # enhance_diagnosis_with_llm() orchestration

tests/test_llm_config.py                          # Config precedence chain
tests/test_llm_client.py                          # Factory dispatch + mocked client tests
tests/test_llm_schemas.py                         # JSON schema validation
tests/test_llm_prompts.py                         # Prompt builders + budget enforcement
tests/test_llm_merge.py                           # Pure merge logic
tests/test_llm_enhancer.py                        # End-to-end orchestration with stub client
tests/test_store_llm.py                           # New store methods + migration round-trip
tests/test_llm_live.py                            # Gated live integration test
```

**Modified files:**

```
src/afteragent/store.py                           # source columns on diagnoses/interventions; llm_generations table; new methods
src/afteragent/diagnostics.py                     # build_interventions optional llm_interventions param; persist_llm_enhanced_diagnosis
src/afteragent/cli.py                             # `enhance` subcommand; --enhance/--no-enhance on exec
src/afteragent/config.py                          # AppPaths.config_path field
src/afteragent/models.py                          # (no change expected — TranscriptEventRow already exists)
pyproject.toml                                    # [project.optional-dependencies] extras
scripts/e2e_matrix.sh                             # New pytest block for test_llm_*.py
README.md                                         # Ollama dogfood recipe section (optional, do if time)
```

**Unchanged (explicit):** `capture.py`, `adapters.py`, `workflow.py` (mostly — one small read-through change), `ui.py`, `transcripts.py`, `github.py`.

## File responsibilities

- **`llm/__init__.py`** — public exports only. `LLMClient`, `LLMConfig`, `get_client`, `load_config`, `enhance_diagnosis_with_llm`. Nothing else.
- **`llm/config.py`** — `LLMConfig` dataclass + `load_config(paths, cli_overrides) -> LLMConfig | None`. Walks CLI → env → toml → auto-detect. No provider-specific knowledge. No network.
- **`llm/cost_table.py`** — static dict `COST_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]]` plus `estimate_cost(provider, model, input_tokens, output_tokens) -> float`. Stdlib only.
- **`llm/client.py`** — `StructuredResponse` dataclass + `LLMClient` Protocol + `get_client(config) -> LLMClient` factory with lazy imports. No provider-specific imports at module load time.
- **`llm/anthropic_client.py`** — `AnthropicClient` implements `LLMClient` via the `anthropic` SDK and `tool_use` forcing. Imports `anthropic` at instantiation time, not at module top.
- **`llm/openai_client.py`** — `OpenAICompatClient` implements `LLMClient` via the `openai` SDK and `response_format={"type": "json_schema", "strict": True}`. Works for OpenAI, OpenRouter, and Ollama by varying `base_url`. Imports `openai` at instantiation time.
- **`llm/schemas.py`** — two module-level dicts: `FINDINGS_SCHEMA`, `INTERVENTIONS_SCHEMA`. Pure data. No functions.
- **`llm/prompts.py`** — `DiagnosisContext` dataclass + `load_diagnosis_context(store, run_id)` loader + `build_diagnosis_prompt(context)` + `build_interventions_prompt(context, merged_findings)`. Returns `(system, user)` tuples. Enforces the token budget. No LLM calls.
- **`llm/merge.py`** — pure functions: `merge_findings(rule_findings, llm_findings) -> list[FindingWithSource]` using the `origin` field. No side effects.
- **`llm/enhancer.py`** — `enhance_diagnosis_with_llm(store, run_id, client, config) -> EnhanceResult`. The orchestrator — loads context, calls client twice, merges, persists, records generations. Catches all exceptions and converts them to `diagnosis_error` findings + failed generation rows.

---

Plan is 14 tasks. I'll split the plan file into two writes to stay within output budget — tasks 1-7 in this file write, tasks 8-14 appended in the next write.

## Task 1: Store migrations for LLM support

**Files:**
- Modify: `src/afteragent/store.py`
- Create: `tests/test_store_llm.py`

Goal: add `source` column to `diagnoses` and `interventions` tables (defaulting to `"rule"`), create the new `llm_generations` table, and add the new CRUD methods `replace_llm_diagnosis`, `record_llm_generation`, `get_llm_generations`. Update existing `replace_diagnosis` to tag its writes with `source="rule"`.

- [ ] **Step 1.1: Write the failing test**

Create `tests/test_store_llm.py`:

```python
import tempfile
from pathlib import Path

import pytest

from afteragent.config import resolve_paths
from afteragent.models import Intervention, PatternFinding
from afteragent.store import Store


def _make_store(tmp: Path) -> Store:
    return Store(resolve_paths(tmp))


def _seed_run(store: Store, run_id: str = "run1") -> None:
    store.create_run(run_id, "echo hi", "/tmp", "2026-04-10T12:00:00Z")


def test_diagnoses_table_has_source_column_defaulting_to_rule():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        finding = PatternFinding(
            code="low_diff_overlap",
            title="Diff misses failing files",
            severity="high",
            summary="…",
            evidence=["a.py", "b.py"],
        )
        store.replace_diagnosis(
            "run1",
            [{
                "run_id": "run1",
                "code": finding.code,
                "title": finding.title,
                "severity": finding.severity,
                "summary": finding.summary,
                "evidence_json": '["a.py", "b.py"]',
            }],
            [],
        )

        rows = store.get_diagnoses("run1")
        assert len(rows) == 1
        assert rows[0]["source"] == "rule"


def test_interventions_table_has_source_column_defaulting_to_rule():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        store.replace_diagnosis(
            "run1",
            [],
            [{
                "run_id": "run1",
                "type": "prompt_patch",
                "title": "Test intervention",
                "target": "task_prompt",
                "content": "do the thing",
                "scope": "pr",
            }],
        )

        rows = store.get_interventions("run1")
        assert len(rows) == 1
        assert rows[0]["source"] == "rule"


def test_replace_llm_diagnosis_writes_with_llm_source_tag():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        store.replace_llm_diagnosis(
            "run1",
            findings_rows=[{
                "run_id": "run1",
                "code": "novel_agent_loop",
                "title": "Agent stuck in read-edit loop",
                "severity": "high",
                "summary": "Agent edited a.py 4 times without reading test output",
                "evidence_json": '["edit 1", "edit 2"]',
                "source": "llm",
            }],
            interventions_rows=[{
                "run_id": "run1",
                "type": "prompt_patch",
                "title": "Read test output between edits",
                "target": "task_prompt",
                "content": "After each edit, run the failing test and read its output before editing again.",
                "scope": "pr",
                "source": "llm",
            }],
        )

        diagnosis_rows = store.get_diagnoses("run1")
        assert len(diagnosis_rows) == 1
        assert diagnosis_rows[0]["source"] == "llm"
        assert diagnosis_rows[0]["code"] == "novel_agent_loop"

        intervention_rows = store.get_interventions("run1")
        assert len(intervention_rows) == 1
        assert intervention_rows[0]["source"] == "llm"


def test_record_llm_generation_creates_row_with_expected_fields():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        store.record_llm_generation(
            run_id="run1",
            kind="findings",
            provider="anthropic",
            model="claude-sonnet-4-5",
            input_tokens=1234,
            output_tokens=456,
            duration_ms=2100,
            estimated_cost_usd=0.0123,
            status="success",
            error_message=None,
            created_at="2026-04-10T12:00:00Z",
            raw_response_excerpt='{"findings": [...]}',
        )

        rows = store.get_llm_generations("run1")
        assert len(rows) == 1
        r = rows[0]
        assert r["kind"] == "findings"
        assert r["provider"] == "anthropic"
        assert r["model"] == "claude-sonnet-4-5"
        assert r["input_tokens"] == 1234
        assert r["output_tokens"] == 456
        assert r["duration_ms"] == 2100
        assert abs(r["estimated_cost_usd"] - 0.0123) < 1e-9
        assert r["status"] == "success"
        assert r["error_message"] is None


def test_record_llm_generation_error_row_persists_message():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        store.record_llm_generation(
            run_id="run1",
            kind="interventions",
            provider="openai",
            model="gpt-4o-mini",
            input_tokens=800,
            output_tokens=0,
            duration_ms=450,
            estimated_cost_usd=0.0002,
            status="error",
            error_message="rate_limit: please retry",
            created_at="2026-04-10T12:00:00Z",
            raw_response_excerpt="",
        )

        rows = store.get_llm_generations("run1")
        assert len(rows) == 1
        assert rows[0]["status"] == "error"
        assert rows[0]["error_message"] == "rate_limit: please retry"


def test_get_llm_generations_orders_by_creation_time_ascending():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        for idx, ts in enumerate(
            ["2026-04-10T12:00:00Z", "2026-04-10T12:00:05Z", "2026-04-10T12:00:10Z"]
        ):
            store.record_llm_generation(
                run_id="run1",
                kind="findings" if idx % 2 == 0 else "interventions",
                provider="anthropic",
                model="claude-sonnet-4-5",
                input_tokens=100,
                output_tokens=50,
                duration_ms=200,
                estimated_cost_usd=0.001,
                status="success",
                error_message=None,
                created_at=ts,
                raw_response_excerpt="",
            )

        rows = store.get_llm_generations("run1")
        assert [r["created_at"] for r in rows] == [
            "2026-04-10T12:00:00Z",
            "2026-04-10T12:00:05Z",
            "2026-04-10T12:00:10Z",
        ]


def test_replace_llm_diagnosis_overwrites_prior_llm_rows_only():
    """Calling replace_llm_diagnosis twice replaces the llm-tagged rows but
    must not touch rule-tagged rows from a prior rule-based diagnosis pass."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        store = _make_store(tmp)
        _seed_run(store)

        # Seed rule-based findings + interventions first.
        store.replace_diagnosis(
            "run1",
            [{
                "run_id": "run1",
                "code": "rule_only_finding",
                "title": "Rule finding",
                "severity": "medium",
                "summary": "…",
                "evidence_json": "[]",
            }],
            [{
                "run_id": "run1",
                "type": "prompt_patch",
                "title": "Rule intervention",
                "target": "task_prompt",
                "content": "…",
                "scope": "pr",
            }],
        )

        # Now write LLM findings. This should NOT delete the rule rows.
        store.replace_llm_diagnosis(
            "run1",
            findings_rows=[{
                "run_id": "run1",
                "code": "llm_finding_v1",
                "title": "LLM finding v1",
                "severity": "low",
                "summary": "…",
                "evidence_json": "[]",
                "source": "llm",
            }],
            interventions_rows=[],
        )

        all_findings = store.get_diagnoses("run1")
        sources = sorted(r["source"] for r in all_findings)
        assert sources == ["llm", "rule"]
        assert len(all_findings) == 2

        # Now call replace_llm_diagnosis again — the v1 llm row should be
        # replaced by v2, rule row untouched.
        store.replace_llm_diagnosis(
            "run1",
            findings_rows=[{
                "run_id": "run1",
                "code": "llm_finding_v2",
                "title": "LLM finding v2",
                "severity": "low",
                "summary": "…",
                "evidence_json": "[]",
                "source": "llm",
            }],
            interventions_rows=[],
        )

        all_findings = store.get_diagnoses("run1")
        codes = sorted(r["code"] for r in all_findings)
        assert codes == ["llm_finding_v2", "rule_only_finding"]
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/test_store_llm.py -v`
Expected: FAIL — `source` column does not exist yet, `replace_llm_diagnosis`/`record_llm_generation`/`get_llm_generations` methods do not exist.

- [ ] **Step 1.3: Add migrations and methods to `store.py`**

In `src/afteragent/store.py`, inside the `_init_db` method, the existing `executescript` block defines all the tables. Append the new `llm_generations` table at the end of the existing SQL (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS llm_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    raw_response_excerpt TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_llm_generations_run ON llm_generations (run_id);
```

Still inside `_init_db`, after the `executescript(...)` call but before the function returns, add the `source` column migration using the existing `_ensure_column` helper. Find the existing line:

```python
self._ensure_column(conn, "interventions", "scope", "TEXT NOT NULL DEFAULT 'pr'")
```

And add these two lines right after it:

```python
            self._ensure_column(conn, "diagnoses", "source", "TEXT NOT NULL DEFAULT 'rule'")
            self._ensure_column(conn, "interventions", "source", "TEXT NOT NULL DEFAULT 'rule'")
```

Update the existing `replace_diagnosis` method to insert with an explicit `source='rule'` value. Find the existing INSERT statements in that method and change:

```python
                """
                INSERT INTO diagnoses (run_id, code, title, severity, summary, evidence_json)
                VALUES (:run_id, :code, :title, :severity, :summary, :evidence_json)
                """,
```

to:

```python
                """
                INSERT INTO diagnoses (run_id, code, title, severity, summary, evidence_json, source)
                VALUES (:run_id, :code, :title, :severity, :summary, :evidence_json, 'rule')
                """,
```

And similarly for the interventions INSERT:

```python
                """
                INSERT INTO interventions (run_id, type, title, target, content, scope)
                VALUES (:run_id, :type, :title, :target, :content, :scope)
                """,
```

becomes:

```python
                """
                INSERT INTO interventions (run_id, type, title, target, content, scope, source)
                VALUES (:run_id, :type, :title, :target, :content, :scope, 'rule')
                """,
```

Update the existing `get_diagnoses` and `get_interventions` methods to include the `source` column in their SELECT. Find `get_diagnoses`:

```python
                SELECT code, title, severity, summary, evidence_json
                FROM diagnoses
                WHERE run_id = ?
                ORDER BY id ASC
```

Change to:

```python
                SELECT code, title, severity, summary, evidence_json, source
                FROM diagnoses
                WHERE run_id = ?
                ORDER BY id ASC
```

And find `get_interventions`:

```python
                SELECT type, title, target, content, scope
                FROM interventions
                WHERE run_id = ?
                ORDER BY id ASC
```

Change to:

```python
                SELECT type, title, target, content, scope, source
                FROM interventions
                WHERE run_id = ?
                ORDER BY id ASC
```

Now add the three new methods. Place them after the existing `get_interventions` method:

```python
    def replace_llm_diagnosis(
        self,
        run_id: str,
        findings_rows: list[dict],
        interventions_rows: list[dict],
    ) -> None:
        """Replace only the LLM-sourced findings and interventions for a run.

        Unlike replace_diagnosis (which replaces everything for the run),
        this method only deletes rows with source='llm' before inserting new
        ones. Rule-based findings/interventions from a prior analyze_run pass
        are preserved untouched.
        """
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM diagnoses WHERE run_id = ? AND source = 'llm'",
                (run_id,),
            )
            conn.execute(
                "DELETE FROM interventions WHERE run_id = ? AND source = 'llm'",
                (run_id,),
            )
            if findings_rows:
                conn.executemany(
                    """
                    INSERT INTO diagnoses (
                        run_id, code, title, severity, summary, evidence_json, source
                    )
                    VALUES (
                        :run_id, :code, :title, :severity, :summary, :evidence_json, :source
                    )
                    """,
                    findings_rows,
                )
            if interventions_rows:
                conn.executemany(
                    """
                    INSERT INTO interventions (
                        run_id, type, title, target, content, scope, source
                    )
                    VALUES (
                        :run_id, :type, :title, :target, :content, :scope, :source
                    )
                    """,
                    interventions_rows,
                )

    def record_llm_generation(
        self,
        run_id: str,
        kind: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        estimated_cost_usd: float,
        status: str,
        error_message: str | None,
        created_at: str,
        raw_response_excerpt: str,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_generations (
                    run_id, kind, provider, model,
                    input_tokens, output_tokens, duration_ms,
                    estimated_cost_usd, status, error_message,
                    created_at, raw_response_excerpt
                )
                VALUES (
                    :run_id, :kind, :provider, :model,
                    :input_tokens, :output_tokens, :duration_ms,
                    :estimated_cost_usd, :status, :error_message,
                    :created_at, :raw_response_excerpt
                )
                """,
                {
                    "run_id": run_id,
                    "kind": kind,
                    "provider": provider,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "duration_ms": duration_ms,
                    "estimated_cost_usd": estimated_cost_usd,
                    "status": status,
                    "error_message": error_message,
                    "created_at": created_at,
                    "raw_response_excerpt": raw_response_excerpt,
                },
            )

    def get_llm_generations(self, run_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, kind, provider, model,
                       input_tokens, output_tokens, duration_ms,
                       estimated_cost_usd, status, error_message,
                       created_at, raw_response_excerpt
                FROM llm_generations
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return rows
```

- [ ] **Step 1.4: Run tests**

Run: `pytest tests/test_store_llm.py -v`
Expected: PASS — 7 new tests pass.

Then run the full suite:
Run: `pytest -v`
Expected: PASS — 91 existing + 7 new = 98 tests.

- [ ] **Step 1.5: Commit**

```bash
git add src/afteragent/store.py tests/test_store_llm.py
git commit -m "$(cat <<'EOF'
Add LLM storage foundation: source tags and llm_generations

Additive migration: diagnoses and interventions tables gain a source
column defaulting to 'rule' for existing rows. New llm_generations
table with per-call token counts, cost estimates, and error messages.
New Store methods replace_llm_diagnosis, record_llm_generation,
get_llm_generations.

replace_llm_diagnosis deletes only source='llm' rows before inserting,
so rule-based findings from a prior analyze_run pass are preserved
when LLM enhancement runs.

Sub-project 2 task 1/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: LLM config loading

**Files:**
- Modify: `src/afteragent/config.py`
- Create: `src/afteragent/llm/__init__.py`
- Create: `src/afteragent/llm/config.py`
- Create: `tests/test_llm_config.py`

Goal: add `config_path` to `AppPaths`, define the `LLMConfig` dataclass, implement the precedence chain (CLI → env → toml → auto-detect).

- [ ] **Step 2.1: Update `AppPaths`**

In `src/afteragent/config.py`, add the `config_path` field to the dataclass and populate it in `resolve_paths`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    db_path: Path
    artifacts_dir: Path
    exports_dir: Path
    applied_dir: Path
    replays_dir: Path
    config_path: Path


def resolve_paths(base_dir: Path | None = None) -> AppPaths:
    root = (base_dir or Path.cwd()) / ".afteragent"
    return AppPaths(
        root=root,
        db_path=root / "afteragent.sqlite3",
        artifacts_dir=root / "artifacts",
        exports_dir=root / "exports",
        applied_dir=root / "applied",
        replays_dir=root / "replays",
        config_path=root / "config.toml",
    )
```

- [ ] **Step 2.2: Create the package skeleton**

Create `src/afteragent/llm/__init__.py` with minimal content (will be filled in later tasks):

```python
"""LLM client abstraction, config, prompts, and diagnosis enhancer."""

from .config import LLMConfig, load_config

__all__ = ["LLMConfig", "load_config"]
```

- [ ] **Step 2.3: Write the failing test for config loading**

Create `tests/test_llm_config.py`:

```python
import tempfile
from pathlib import Path

import pytest

from afteragent.config import resolve_paths
from afteragent.llm.config import LLMConfig, load_config


def _paths(tmp: Path):
    return resolve_paths(tmp)


def test_returns_none_when_nothing_configured(tmp_path, monkeypatch):
    # Unset all relevant env vars.
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is None


def test_autodetect_anthropic_when_only_anthropic_key_set(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-sonnet-4-5"
    assert cfg.api_key == "sk-ant-test"
    assert cfg.auto_enhance_on_exec is False


def test_autodetect_openai_when_only_openai_key_set(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.api_key == "sk-oai-test"


def test_autodetect_openrouter_when_only_openrouter_key_set(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "openrouter"
    assert cfg.model == "anthropic/claude-3.5-sonnet"
    assert cfg.api_key == "sk-or-test"
    assert cfg.base_url == "https://openrouter.ai/api/v1"


def test_anthropic_precedence_over_openai_in_autodetect(tmp_path, monkeypatch):
    for var in [
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

    paths = _paths(tmp_path)
    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "anthropic"


def test_config_file_overrides_autodetect(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-opus-4-6"\n'
        'auto_enhance_on_exec = true\n'
        'max_tokens = 8192\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-opus-4-6"
    assert cfg.auto_enhance_on_exec is True
    assert cfg.max_tokens == 8192


def test_env_var_overrides_config_file(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AFTERAGENT_LLM_MODEL", "claude-haiku-4-5")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-opus-4-6"\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.model == "claude-haiku-4-5"  # env wins


def test_cli_overrides_win_over_env_and_config(tmp_path, monkeypatch):
    for var in [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("AFTERAGENT_LLM_MODEL", "claude-haiku-4-5")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-opus-4-6"\n'
    )

    cfg = load_config(paths, cli_overrides={"model": "claude-sonnet-4-5"})
    assert cfg is not None
    assert cfg.model == "claude-sonnet-4-5"


def test_ollama_needs_no_api_key(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "qwen2.5-coder:7b"\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.provider == "ollama"
    assert cfg.api_key is None
    assert cfg.base_url == "http://localhost:11434/v1"


def test_ollama_base_url_override_from_env(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote-ollama:11434/v1")

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "ollama"\n'
        'model = "qwen2.5-coder:7b"\n'
    )

    cfg = load_config(paths)
    assert cfg is not None
    assert cfg.base_url == "http://remote-ollama:11434/v1"


def test_missing_api_key_for_configured_provider_returns_none(tmp_path, monkeypatch):
    for var in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL",
        "AFTERAGENT_LLM_PROVIDER",
        "AFTERAGENT_LLM_MODEL",
        "AFTERAGENT_LLM_BASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)

    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        '[llm]\n'
        'provider = "anthropic"\n'
        'model = "claude-sonnet-4-5"\n'
    )

    cfg = load_config(paths)
    # Provider is anthropic but no ANTHROPIC_API_KEY set → None.
    assert cfg is None
```

- [ ] **Step 2.4: Run tests to verify they fail**

Run: `pytest tests/test_llm_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'afteragent.llm.config'`.

- [ ] **Step 2.5: Implement `LLMConfig` and `load_config`**

Create `src/afteragent/llm/config.py`:

```python
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..config import AppPaths

# Default model per provider when auto-detect fires.
_AUTODETECT_DEFAULTS = {
    "anthropic": ("claude-sonnet-4-5", None),
    "openai": ("gpt-4o-mini", None),
    "openrouter": ("anthropic/claude-3.5-sonnet", "https://openrouter.ai/api/v1"),
    "ollama": ("llama3.1:8b", "http://localhost:11434/v1"),
}

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"

# Maps provider name → the env var that holds its API key.
_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


@dataclass(slots=True, frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout_s: float = 60.0
    auto_enhance_on_exec: bool = False


def load_config(
    paths: AppPaths,
    cli_overrides: dict | None = None,
) -> LLMConfig | None:
    """Walk the precedence chain (CLI → env → toml → auto-detect).

    Returns None if no provider is configured and no auto-detect branch hit,
    OR if a provider is configured but its API key is missing.
    """
    cli_overrides = cli_overrides or {}

    # Step 1: start with whatever the config file says (or an empty dict).
    file_data = _load_config_file(paths.config_path)

    # Step 2: merge env var overrides on top.
    env_provider = os.environ.get("AFTERAGENT_LLM_PROVIDER")
    env_model = os.environ.get("AFTERAGENT_LLM_MODEL")
    env_base_url = os.environ.get("AFTERAGENT_LLM_BASE_URL")

    provider = cli_overrides.get("provider") or env_provider or file_data.get("provider")
    model = cli_overrides.get("model") or env_model or file_data.get("model")
    base_url = cli_overrides.get("base_url") or env_base_url or file_data.get("base_url")
    auto_enhance_on_exec = bool(file_data.get("auto_enhance_on_exec", False))
    max_tokens = int(file_data.get("max_tokens", 4096))
    temperature = float(file_data.get("temperature", 0.2))
    timeout_s = float(file_data.get("timeout_s", 60.0))

    # Step 3: if provider is still not set, try auto-detect.
    if provider is None:
        provider, default_model, default_base_url = _autodetect()
        if provider is None:
            return None
        if model is None:
            model = default_model
        if base_url is None:
            base_url = default_base_url

    # Step 4: fill in a default base_url for providers that need one.
    if base_url is None and provider == "openrouter":
        base_url = _AUTODETECT_DEFAULTS["openrouter"][1]
    if provider == "ollama":
        base_url = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or _OLLAMA_DEFAULT_BASE_URL
        )
    if provider == "ollama" and os.environ.get("OLLAMA_BASE_URL"):
        base_url = os.environ["OLLAMA_BASE_URL"]

    # Step 5: resolve api_key. Ollama does not require one.
    api_key: str | None = None
    if provider in _API_KEY_ENV:
        api_key = os.environ.get(_API_KEY_ENV[provider])
        if api_key is None:
            # Provider configured but no key → bail.
            return None

    if model is None:
        # Provider set but no model → fall back to the autodetect default.
        model = _AUTODETECT_DEFAULTS.get(provider, (None, None))[0]
        if model is None:
            return None

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout_s=timeout_s,
        auto_enhance_on_exec=auto_enhance_on_exec,
    )


def _load_config_file(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data.get("llm", {}) or {}


def _autodetect() -> tuple[str | None, str | None, str | None]:
    """Pick a provider based on which env vars are present.

    Priority: anthropic > openai > openrouter > ollama (reachable).
    Returns (provider, default_model, default_base_url) or (None, None, None).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        provider = "anthropic"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, base)
    if os.environ.get("OPENAI_API_KEY"):
        provider = "openai"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, base)
    if os.environ.get("OPENROUTER_API_KEY"):
        provider = "openrouter"
        model, base = _AUTODETECT_DEFAULTS[provider]
        return (provider, model, base)
    if os.environ.get("OLLAMA_BASE_URL"):
        provider = "ollama"
        model, base = _AUTODETECT_DEFAULTS[provider]
        # Use the env var's base_url, not the default.
        return (provider, model, os.environ["OLLAMA_BASE_URL"])
    return (None, None, None)
```

- [ ] **Step 2.6: Run tests**

Run: `pytest tests/test_llm_config.py -v`
Expected: PASS — 11 tests pass.

Then run the full suite:
Run: `pytest -v`
Expected: PASS — 98 existing + 11 new = 109 tests.

- [ ] **Step 2.7: Commit**

```bash
git add src/afteragent/config.py src/afteragent/llm/__init__.py src/afteragent/llm/config.py tests/test_llm_config.py
git commit -m "$(cat <<'EOF'
Add LLMConfig and load_config precedence chain

New src/afteragent/llm/ package with LLMConfig dataclass and a
load_config() that walks CLI overrides → env vars → .afteragent/config.toml
→ auto-detect. Auto-detect priority: anthropic > openai > openrouter
> ollama-reachable.

Ollama is handled specially: it needs no API key, and base_url can
come from either OLLAMA_BASE_URL env var or the toml file.

AppPaths gains a config_path field.

Sub-project 2 task 2/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Cost table and pricing helper

**Files:**
- Create: `src/afteragent/llm/cost_table.py`
- Create: `tests/test_llm_cost_table.py` (small, bundled at the end of test_llm_config.py OR as its own file)

Goal: a static pricing table for the models we support, with an `estimate_cost` helper that returns 0 for unknown/local models.

- [ ] **Step 3.1: Write the failing test**

Create `tests/test_llm_cost_table.py`:

```python
from afteragent.llm.cost_table import estimate_cost


def test_estimate_cost_anthropic_sonnet_4_5():
    # Rough: $3/M input, $15/M output. 1M input + 500k output.
    cost = estimate_cost("anthropic", "claude-sonnet-4-5", 1_000_000, 500_000)
    assert abs(cost - (3.0 + 7.5)) < 0.01


def test_estimate_cost_anthropic_haiku_4_5_is_cheaper_than_sonnet():
    sonnet = estimate_cost("anthropic", "claude-sonnet-4-5", 100_000, 20_000)
    haiku = estimate_cost("anthropic", "claude-haiku-4-5", 100_000, 20_000)
    assert haiku < sonnet
    assert haiku > 0


def test_estimate_cost_openai_gpt_4o_mini():
    cost = estimate_cost("openai", "gpt-4o-mini", 100_000, 20_000)
    assert cost > 0
    assert cost < 1.0  # sanity — gpt-4o-mini is very cheap


def test_estimate_cost_ollama_is_always_zero():
    cost = estimate_cost("ollama", "llama3.1:8b", 1_000_000, 1_000_000)
    assert cost == 0.0


def test_estimate_cost_ollama_with_unknown_model_is_still_zero():
    cost = estimate_cost("ollama", "some-custom-tune:v2", 100, 100)
    assert cost == 0.0


def test_estimate_cost_unknown_provider_returns_zero():
    cost = estimate_cost("made-up", "model-x", 100_000, 20_000)
    assert cost == 0.0


def test_estimate_cost_unknown_model_on_known_provider_returns_zero():
    cost = estimate_cost("anthropic", "claude-future-model-9", 100_000, 20_000)
    assert cost == 0.0


def test_estimate_cost_scales_linearly_with_tokens():
    base = estimate_cost("anthropic", "claude-sonnet-4-5", 10_000, 5_000)
    doubled = estimate_cost("anthropic", "claude-sonnet-4-5", 20_000, 10_000)
    assert abs(doubled - 2 * base) < 1e-6
```

- [ ] **Step 3.2: Run to verify it fails**

Run: `pytest tests/test_llm_cost_table.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.cost_table'`.

- [ ] **Step 3.3: Implement cost_table**

Create `src/afteragent/llm/cost_table.py`:

```python
from __future__ import annotations

# Per-1000-token pricing in USD. Source: provider public pricing pages as of
# 2026-04. Update when providers change their rates.
#
# Format: (provider, model) -> (input_usd_per_1k, output_usd_per_1k)
#
# Ollama entries are omitted entirely — all Ollama costs are 0.
COST_PER_1K_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic
    ("anthropic", "claude-opus-4-6"): (0.015, 0.075),
    ("anthropic", "claude-sonnet-4-5"): (0.003, 0.015),
    ("anthropic", "claude-haiku-4-5"): (0.0008, 0.004),
    ("anthropic", "claude-3-5-sonnet-20241022"): (0.003, 0.015),
    ("anthropic", "claude-3-5-haiku-20241022"): (0.001, 0.005),

    # OpenAI
    ("openai", "gpt-4o"): (0.005, 0.015),
    ("openai", "gpt-4o-mini"): (0.00015, 0.0006),
    ("openai", "o1-preview"): (0.015, 0.060),
    ("openai", "o1-mini"): (0.003, 0.012),

    # OpenRouter — prices vary by underlying model. These are common aliases.
    ("openrouter", "anthropic/claude-3.5-sonnet"): (0.003, 0.015),
    ("openrouter", "anthropic/claude-3.5-haiku"): (0.001, 0.005),
    ("openrouter", "openai/gpt-4o"): (0.005, 0.015),
    ("openrouter", "openai/gpt-4o-mini"): (0.00015, 0.0006),
    ("openrouter", "meta-llama/llama-3.1-70b-instruct"): (0.00059, 0.00079),
    ("openrouter", "meta-llama/llama-3.1-8b-instruct"): (0.00018, 0.00018),
}


def estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Return the estimated USD cost for a single LLM call.

    Ollama always returns 0.0 (local inference). Unknown (provider, model)
    combinations also return 0.0 — we never guess.
    """
    if provider == "ollama":
        return 0.0
    rates = COST_PER_1K_TOKENS.get((provider, model))
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    return (input_tokens / 1000.0) * input_rate + (output_tokens / 1000.0) * output_rate
```

- [ ] **Step 3.4: Run tests**

Run: `pytest tests/test_llm_cost_table.py tests/test_llm_config.py -v`
Expected: PASS — 8 + 11 = 19 tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add src/afteragent/llm/cost_table.py tests/test_llm_cost_table.py
git commit -m "$(cat <<'EOF'
Add per-model cost estimation table

Static USD/1k-token rates for Anthropic, OpenAI, and OpenRouter
models we currently support. Ollama always returns 0. Unknown
(provider, model) combinations return 0 — we never guess.

Sub-project 2 task 3/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: LLMClient Protocol and factory

**Files:**
- Create: `src/afteragent/llm/client.py`
- Create: `tests/test_llm_client.py`

Goal: define the `LLMClient` Protocol, `StructuredResponse` dataclass, and a `get_client(config)` factory with lazy imports. This task adds the abstraction but does NOT implement `AnthropicClient` or `OpenAICompatClient` yet — those are Task 5.

- [ ] **Step 4.1: Write the failing test**

Create `tests/test_llm_client.py`:

```python
from unittest.mock import patch

import pytest

from afteragent.llm.client import StructuredResponse, get_client
from afteragent.llm.config import LLMConfig


def _make_config(provider: str, api_key: str = "fake-key") -> LLMConfig:
    return LLMConfig(
        provider=provider,
        model="model-x",
        api_key=api_key if provider != "ollama" else None,
        base_url="http://localhost:11434/v1" if provider == "ollama" else None,
    )


def test_structured_response_dataclass_shape():
    r = StructuredResponse(
        data={"findings": []},
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-5",
        provider="anthropic",
        duration_ms=1200,
        raw_response_excerpt='{"findings": []}',
    )
    assert r.data == {"findings": []}
    assert r.input_tokens == 100
    assert r.output_tokens == 50
    assert r.provider == "anthropic"


def test_get_client_dispatches_anthropic_to_anthropic_client():
    cfg = _make_config("anthropic")
    # Stub the anthropic_client module so we don't need the SDK.
    with patch("afteragent.llm.client._build_anthropic_client") as build:
        build.return_value = object()
        client = get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_dispatches_openai_to_openai_compat_client():
    cfg = _make_config("openai")
    with patch("afteragent.llm.client._build_openai_compat_client") as build:
        build.return_value = object()
        get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_dispatches_openrouter_to_openai_compat_client():
    cfg = _make_config("openrouter")
    with patch("afteragent.llm.client._build_openai_compat_client") as build:
        build.return_value = object()
        get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_dispatches_ollama_to_openai_compat_client():
    cfg = _make_config("ollama")
    with patch("afteragent.llm.client._build_openai_compat_client") as build:
        build.return_value = object()
        get_client(cfg)
        build.assert_called_once_with(cfg)


def test_get_client_unknown_provider_raises():
    cfg = LLMConfig(provider="unknown", model="x", api_key="k", base_url=None)
    with pytest.raises(ValueError, match="Unknown provider"):
        get_client(cfg)


def test_get_client_missing_anthropic_sdk_raises_clear_error():
    cfg = _make_config("anthropic")
    # Simulate the SDK being missing by raising ImportError from the builder.
    with patch(
        "afteragent.llm.client._build_anthropic_client",
        side_effect=ImportError("No module named 'anthropic'"),
    ):
        with pytest.raises(ImportError, match="afteragent\\[anthropic\\]"):
            get_client(cfg)


def test_get_client_missing_openai_sdk_raises_clear_error():
    cfg = _make_config("openai")
    with patch(
        "afteragent.llm.client._build_openai_compat_client",
        side_effect=ImportError("No module named 'openai'"),
    ):
        with pytest.raises(ImportError, match="afteragent\\[openai\\]"):
            get_client(cfg)
```

- [ ] **Step 4.2: Run to verify it fails**

Run: `pytest tests/test_llm_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.client'`.

- [ ] **Step 4.3: Implement `client.py`**

Create `src/afteragent/llm/client.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import LLMConfig


@dataclass(slots=True)
class StructuredResponse:
    data: dict
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    duration_ms: int
    raw_response_excerpt: str


class LLMClient(Protocol):
    """Runtime-dispatched LLM client. Both implementations return the same
    StructuredResponse shape so callers never see provider-specific types."""

    name: str
    model: str

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse: ...


def get_client(config: LLMConfig) -> LLMClient:
    """Factory: pick the right client implementation for the configured provider.

    Uses lazy imports so users who installed `afteragent[anthropic]` but not
    `afteragent[openai]` don't fail at import time — only at instantiation
    time, and only when they try to use the missing provider.
    """
    if config.provider == "anthropic":
        try:
            return _build_anthropic_client(config)
        except ImportError as exc:
            raise ImportError(
                f"Provider 'anthropic' requires `pip install afteragent[anthropic]`. "
                f"Underlying error: {exc}"
            ) from exc
    if config.provider in ("openai", "openrouter", "ollama"):
        try:
            return _build_openai_compat_client(config)
        except ImportError as exc:
            raise ImportError(
                f"Provider '{config.provider}' requires `pip install afteragent[openai]`. "
                f"Underlying error: {exc}"
            ) from exc
    raise ValueError(f"Unknown provider: {config.provider}")


def _build_anthropic_client(config: LLMConfig) -> LLMClient:
    """Lazy import + construction of the Anthropic client."""
    from .anthropic_client import AnthropicClient
    return AnthropicClient(config)


def _build_openai_compat_client(config: LLMConfig) -> LLMClient:
    """Lazy import + construction of the OpenAI-compatible client."""
    from .openai_client import OpenAICompatClient
    return OpenAICompatClient(config)
```

- [ ] **Step 4.4: Run tests**

Run: `pytest tests/test_llm_client.py -v`
Expected: FAIL on the `_build_anthropic_client` / `_build_openai_compat_client` dispatch tests — the imports will fail because `anthropic_client.py` and `openai_client.py` don't exist yet.

The test patches `_build_anthropic_client` / `_build_openai_compat_client` directly though, so those tests should pass. The failing cases are the ones that expect `ImportError` with specific messages — those should also pass because the `side_effect=ImportError(...)` triggers the re-raise.

Re-run: `pytest tests/test_llm_client.py -v`
Expected: PASS — all 9 tests pass. The tests mock the lazy-import builders, so `anthropic` and `openai` don't need to be importable.

- [ ] **Step 4.5: Commit**

```bash
git add src/afteragent/llm/client.py tests/test_llm_client.py
git commit -m "$(cat <<'EOF'
Add LLMClient Protocol, StructuredResponse, and get_client factory

Defines the provider-agnostic client interface used by the enhancer.
get_client dispatches on config.provider using lazy imports so that
anthropic and openai SDKs are only imported if the user actually
configures them. Clear error messages tell users which extras to
install when an SDK is missing.

Sub-project 2 task 4/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Anthropic and OpenAI-compat client implementations

**Files:**
- Create: `src/afteragent/llm/anthropic_client.py`
- Create: `src/afteragent/llm/openai_client.py`
- Modify: `tests/test_llm_client.py` (add integration tests with mocked SDKs)

Goal: concrete implementations of both client adapters. Tests mock the underlying SDK modules so they run on clean checkouts without the actual SDKs installed.

- [ ] **Step 5.1: Add failing tests for the client bodies**

Append to `tests/test_llm_client.py`:

```python
import sys
import types
from unittest.mock import MagicMock


def _install_fake_anthropic_module(monkeypatch, mock_client):
    """Install a stub `anthropic` module with a minimal Anthropic class."""
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def _install_fake_openai_module(monkeypatch, mock_client):
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "openai", fake_module)


def test_anthropic_client_forces_tool_use_and_parses_response(monkeypatch):
    # Build a fake anthropic.Anthropic instance whose messages.create returns
    # a shape mimicking the real SDK.
    fake_tool_use = MagicMock()
    fake_tool_use.type = "tool_use"
    fake_tool_use.input = {"findings": [{"code": "test_code"}]}

    fake_response = MagicMock()
    fake_response.content = [fake_tool_use]
    fake_response.usage.input_tokens = 1234
    fake_response.usage.output_tokens = 56

    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value = fake_response

    _install_fake_anthropic_module(monkeypatch, fake_anthropic)

    # Now importing AnthropicClient should succeed and instantiating it
    # should use our fake module.
    from afteragent.llm.anthropic_client import AnthropicClient

    cfg = LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-ant-test",
        base_url=None,
        max_tokens=4096,
        temperature=0.2,
    )
    client = AnthropicClient(cfg)

    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    response = client.call_structured(
        system="You are a diagnostician",
        user="Diagnose this run",
        schema=schema,
        tool_name="report_findings",
    )

    # Verify the SDK was called with forced tool_use.
    fake_anthropic.messages.create.assert_called_once()
    kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-5"
    assert kwargs["max_tokens"] == 4096
    assert kwargs["temperature"] == 0.2
    assert kwargs["system"] == "You are a diagnostician"
    assert kwargs["messages"] == [{"role": "user", "content": "Diagnose this run"}]
    assert kwargs["tools"] == [
        {
            "name": "report_findings",
            "description": "Emit structured report_findings data.",
            "input_schema": schema,
        }
    ]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "report_findings"}

    assert response.data == {"findings": [{"code": "test_code"}]}
    assert response.input_tokens == 1234
    assert response.output_tokens == 56
    assert response.provider == "anthropic"
    assert response.model == "claude-sonnet-4-5"
    assert response.duration_ms >= 0


def test_openai_compat_client_uses_json_schema_response_format(monkeypatch):
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock()]
    fake_completion.choices[0].message.content = '{"findings": [{"code": "x"}]}'
    fake_completion.usage.prompt_tokens = 800
    fake_completion.usage.completion_tokens = 40

    fake_openai = MagicMock()
    fake_openai.chat.completions.create.return_value = fake_completion

    _install_fake_openai_module(monkeypatch, fake_openai)

    from afteragent.llm.openai_client import OpenAICompatClient

    cfg = LLMConfig(
        provider="openai",
        model="gpt-4o-mini",
        api_key="sk-oai-test",
        base_url=None,
        max_tokens=2048,
        temperature=0.1,
    )
    client = OpenAICompatClient(cfg)

    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    response = client.call_structured(
        system="System",
        user="User",
        schema=schema,
        tool_name="report_findings",
    )

    fake_openai.chat.completions.create.assert_called_once()
    kwargs = fake_openai.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["temperature"] == 0.1
    assert kwargs["messages"] == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "User"},
    ]
    assert kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "report_findings",
            "schema": schema,
            "strict": True,
        },
    }

    assert response.data == {"findings": [{"code": "x"}]}
    assert response.input_tokens == 800
    assert response.output_tokens == 40
    assert response.provider == "openai"


def test_openai_compat_client_uses_base_url_for_openrouter(monkeypatch):
    fake_openai = MagicMock()
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock()]
    fake_completion.choices[0].message.content = '{"x": 1}'
    fake_completion.usage.prompt_tokens = 10
    fake_completion.usage.completion_tokens = 5
    fake_openai.chat.completions.create.return_value = fake_completion

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=fake_openai)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    from afteragent.llm.openai_client import OpenAICompatClient

    cfg = LLMConfig(
        provider="openrouter",
        model="anthropic/claude-3.5-sonnet",
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
    )
    OpenAICompatClient(cfg)

    fake_module.OpenAI.assert_called_once_with(
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
    )


def test_openai_compat_client_passes_placeholder_api_key_for_ollama(monkeypatch):
    fake_openai = MagicMock()
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=fake_openai)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    from afteragent.llm.openai_client import OpenAICompatClient

    cfg = LLMConfig(
        provider="ollama",
        model="llama3.1:8b",
        api_key=None,
        base_url="http://localhost:11434/v1",
    )
    OpenAICompatClient(cfg)

    fake_module.OpenAI.assert_called_once()
    call_kwargs = fake_module.OpenAI.call_args.kwargs
    # Ollama does not require a real API key. We pass a placeholder.
    assert call_kwargs["api_key"] != ""
    assert call_kwargs["base_url"] == "http://localhost:11434/v1"


def test_anthropic_client_missing_tool_use_block_raises(monkeypatch):
    # Response without a tool_use block (model refused).
    fake_text_block = MagicMock()
    fake_text_block.type = "text"

    fake_response = MagicMock()
    fake_response.content = [fake_text_block]
    fake_response.usage.input_tokens = 100
    fake_response.usage.output_tokens = 50

    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value = fake_response

    _install_fake_anthropic_module(monkeypatch, fake_anthropic)

    from afteragent.llm.anthropic_client import AnthropicClient

    cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="sk", base_url=None)
    client = AnthropicClient(cfg)

    with pytest.raises(ValueError, match="no tool_use block"):
        client.call_structured(
            system="S", user="U", schema={}, tool_name="report_findings",
        )
```

- [ ] **Step 5.2: Run to verify they fail**

Run: `pytest tests/test_llm_client.py -v -k "anthropic_client or openai_compat"`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.anthropic_client'`.

- [ ] **Step 5.3: Implement `anthropic_client.py`**

Create `src/afteragent/llm/anthropic_client.py`:

```python
from __future__ import annotations

import time

from .client import StructuredResponse
from .config import LLMConfig


class AnthropicClient:
    """LLMClient implementation using the Anthropic Messages API.

    Uses tool_choice={"type": "tool", "name": ...} to force Claude to return
    exactly one tool_use block with input matching the provided schema.
    """

    name = "anthropic"

    def __init__(self, config: LLMConfig):
        import anthropic  # Lazy import — only happens when this class is instantiated.

        self._config = config
        self._sdk = anthropic.Anthropic(api_key=config.api_key)
        self.model = config.model

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse:
        start = time.time()
        response = self._sdk.messages.create(
            model=self.model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": f"Emit structured {tool_name} data.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )

        tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not tool_use_blocks:
            raise ValueError(
                f"Anthropic response contained no tool_use block for tool {tool_name!r}. "
                f"Got content types: {[getattr(b, 'type', '?') for b in response.content]}"
            )

        data = tool_use_blocks[0].input
        return StructuredResponse(
            data=dict(data),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=self.model,
            provider="anthropic",
            duration_ms=int((time.time() - start) * 1000),
            raw_response_excerpt=str(data)[:500],
        )
```

- [ ] **Step 5.4: Implement `openai_client.py`**

Create `src/afteragent/llm/openai_client.py`:

```python
from __future__ import annotations

import json
import time

from .client import StructuredResponse
from .config import LLMConfig

# Ollama does not enforce an API key, but the `openai` SDK requires some
# string. Use a visible placeholder so it's obvious in debugging.
_OLLAMA_PLACEHOLDER_KEY = "ollama-no-auth"


class OpenAICompatClient:
    """LLMClient implementation using the OpenAI-compatible Chat Completions
    API. Works unchanged for OpenAI, OpenRouter, and Ollama by varying
    base_url and api_key.
    """

    name = "openai-compat"

    def __init__(self, config: LLMConfig):
        import openai  # Lazy import.

        self._config = config
        self.model = config.model

        api_key = config.api_key or _OLLAMA_PLACEHOLDER_KEY
        if config.base_url is not None:
            self._sdk = openai.OpenAI(api_key=api_key, base_url=config.base_url)
        else:
            self._sdk = openai.OpenAI(api_key=api_key)

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse:
        start = time.time()
        response = self._sdk.chat.completions.create(
            model=self.model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": tool_name,
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        usage = response.usage
        return StructuredResponse(
            data=data,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            model=self.model,
            provider=self._config.provider,
            duration_ms=int((time.time() - start) * 1000),
            raw_response_excerpt=content[:500],
        )
```

- [ ] **Step 5.5: Run tests**

Run: `pytest tests/test_llm_client.py -v`
Expected: PASS — all 14 tests pass.

Then run the full suite:
Run: `pytest -v`
Expected: PASS — 109 + 14 new + earlier task tests = ~120+ tests. All green.

- [ ] **Step 5.6: Commit**

```bash
git add src/afteragent/llm/anthropic_client.py src/afteragent/llm/openai_client.py tests/test_llm_client.py
git commit -m "$(cat <<'EOF'
Implement AnthropicClient and OpenAICompatClient

AnthropicClient uses the Messages API with tool_choice forcing to
guarantee a structured tool_use block matching the requested schema.
Raises ValueError if the response lacks a tool_use block (e.g. model
refused).

OpenAICompatClient uses Chat Completions with response_format=
{"type":"json_schema","strict":true}. Works for OpenAI, OpenRouter,
and Ollama by varying api_key and base_url. Uses a placeholder key
for Ollama since the SDK requires some value.

Both clients use lazy SDK imports so users with only one extra
installed don't fail at module load time.

Sub-project 2 task 5/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Structured output schemas

**Files:**
- Create: `src/afteragent/llm/schemas.py`
- Create: `tests/test_llm_schemas.py`

Goal: define `FINDINGS_SCHEMA` and `INTERVENTIONS_SCHEMA`. Each schema is validated for well-formedness; handwritten fixture responses are validated against them.

- [ ] **Step 6.1: Add `jsonschema` as a dev dependency**

Edit `pyproject.toml`. Inside the existing `[project.optional-dependencies]` section (if it doesn't exist yet, add one), add:

```toml
[project.optional-dependencies]
dev = ["jsonschema>=4.22"]
```

If `[project.optional-dependencies]` already exists from a prior task, just add the `dev` entry. Install locally with `pip install -e '.[dev]'` for testing.

- [ ] **Step 6.2: Write the failing test**

Create `tests/test_llm_schemas.py`:

```python
import pytest

# jsonschema is a dev dep; tests that need it import lazily.
jsonschema = pytest.importorskip("jsonschema")

from afteragent.llm.schemas import (
    FINDINGS_SCHEMA,
    INTERVENTIONS_SCHEMA,
    VALID_ORIGINS,
    VALID_INTERVENTION_TYPES,
    VALID_INTERVENTION_TARGETS,
    VALID_SCOPES,
    VALID_SEVERITIES,
)


def test_findings_schema_is_valid_json_schema():
    # Raises if the schema itself is malformed.
    jsonschema.Draft202012Validator.check_schema(FINDINGS_SCHEMA)


def test_interventions_schema_is_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(INTERVENTIONS_SCHEMA)


def test_findings_schema_accepts_valid_llm_response():
    valid = {
        "findings": [
            {
                "code": "low_diff_overlap",
                "title": "Agent edited files unrelated to the failure",
                "severity": "high",
                "summary": "The failing test is in tests/test_foo.py but the agent edited src/unrelated.py",
                "evidence": ["tests/test_foo.py::test_add", "src/unrelated.py"],
                "origin": "confirmed_rule",
                "rule_code_ref": "low_diff_overlap_with_failing_files",
            }
        ]
    }
    jsonschema.validate(valid, FINDINGS_SCHEMA)


def test_findings_schema_rejects_missing_origin_field():
    invalid = {
        "findings": [
            {
                "code": "low_diff_overlap",
                "title": "x",
                "severity": "high",
                "summary": "x",
                "evidence": [],
                "rule_code_ref": None,
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, FINDINGS_SCHEMA)


def test_findings_schema_rejects_invalid_severity():
    invalid = {
        "findings": [
            {
                "code": "x",
                "title": "x",
                "severity": "catastrophic",  # not in enum
                "summary": "x",
                "evidence": [],
                "origin": "novel",
                "rule_code_ref": None,
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, FINDINGS_SCHEMA)


def test_findings_schema_rejects_invalid_origin():
    invalid = {
        "findings": [
            {
                "code": "x",
                "title": "x",
                "severity": "low",
                "summary": "x",
                "evidence": [],
                "origin": "made_up",  # not in enum
                "rule_code_ref": None,
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, FINDINGS_SCHEMA)


def test_findings_schema_allows_empty_findings_array():
    valid = {"findings": []}
    jsonschema.validate(valid, FINDINGS_SCHEMA)


def test_findings_schema_enforces_max_items():
    invalid = {
        "findings": [
            {
                "code": f"code_{i}",
                "title": "x",
                "severity": "low",
                "summary": "x",
                "evidence": [],
                "origin": "novel",
                "rule_code_ref": None,
            }
            for i in range(20)  # > 12
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, FINDINGS_SCHEMA)


def test_interventions_schema_accepts_valid_llm_response():
    valid = {
        "interventions": [
            {
                "type": "instruction_patch",
                "title": "Require reading failing tests before editing",
                "target": "repo_instructions",
                "content": "Before editing any file, read the currently failing tests in tests/ and summarize what they assert.",
                "scope": "pr",
                "related_finding_codes": ["low_diff_overlap"],
            }
        ]
    }
    jsonschema.validate(valid, INTERVENTIONS_SCHEMA)


def test_interventions_schema_rejects_invalid_type():
    invalid = {
        "interventions": [
            {
                "type": "made_up_type",  # not in enum
                "title": "x",
                "target": "task_prompt",
                "content": "x",
                "scope": "pr",
                "related_finding_codes": [],
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, INTERVENTIONS_SCHEMA)


def test_interventions_schema_rejects_invalid_target():
    invalid = {
        "interventions": [
            {
                "type": "instruction_patch",
                "title": "x",
                "target": "made_up_target",  # not in enum
                "content": "x",
                "scope": "pr",
                "related_finding_codes": [],
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, INTERVENTIONS_SCHEMA)


def test_exported_enum_constants_match_schema_enums():
    # The VALID_* module-level constants should match the schema enums
    # exactly — single source of truth.
    assert set(VALID_SEVERITIES) == {"low", "medium", "high"}
    assert set(VALID_ORIGINS) == {"confirmed_rule", "rejected_rule", "novel"}
    assert set(VALID_INTERVENTION_TYPES) == {
        "instruction_patch",
        "prompt_patch",
        "context_injection_rule",
        "runtime_guardrail",
        "tool_policy_rule",
    }
    assert set(VALID_INTERVENTION_TARGETS) == {
        "repo_instructions",
        "task_prompt",
        "runner_context",
        "runner_policy",
    }
    assert set(VALID_SCOPES) == {"pr", "repo", "run"}
```

- [ ] **Step 6.3: Run to verify it fails**

Run: `pytest tests/test_llm_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.schemas'`. If jsonschema is not installed, the test file is skipped with `pytest.importorskip` — install it with `pip install 'jsonschema>=4.22'` before proceeding.

- [ ] **Step 6.4: Implement `schemas.py`**

Create `src/afteragent/llm/schemas.py`:

```python
from __future__ import annotations

# Single source of truth for enum values used in both the schemas and the
# enhancer/merge logic. Tests verify these match the embedded schema enums.
VALID_SEVERITIES = ("low", "medium", "high")
VALID_ORIGINS = ("confirmed_rule", "rejected_rule", "novel")
VALID_INTERVENTION_TYPES = (
    "instruction_patch",
    "prompt_patch",
    "context_injection_rule",
    "runtime_guardrail",
    "tool_policy_rule",
)
VALID_INTERVENTION_TARGETS = (
    "repo_instructions",
    "task_prompt",
    "runner_context",
    "runner_policy",
)
VALID_SCOPES = ("pr", "repo", "run")


FINDINGS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "maxLength": 80},
                    "title": {"type": "string", "maxLength": 120},
                    "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
                    "summary": {"type": "string", "maxLength": 500},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 300},
                        "maxItems": 8,
                    },
                    "origin": {"type": "string", "enum": list(VALID_ORIGINS)},
                    "rule_code_ref": {"type": ["string", "null"]},
                },
                "required": [
                    "code",
                    "title",
                    "severity",
                    "summary",
                    "evidence",
                    "origin",
                    "rule_code_ref",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


INTERVENTIONS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "interventions": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(VALID_INTERVENTION_TYPES)},
                    "title": {"type": "string", "maxLength": 120},
                    "target": {"type": "string", "enum": list(VALID_INTERVENTION_TARGETS)},
                    "content": {"type": "string", "maxLength": 2000},
                    "scope": {"type": "string", "enum": list(VALID_SCOPES)},
                    "related_finding_codes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "type",
                    "title",
                    "target",
                    "content",
                    "scope",
                    "related_finding_codes",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["interventions"],
    "additionalProperties": False,
}
```

- [ ] **Step 6.5: Run tests**

Run: `pytest tests/test_llm_schemas.py -v`
Expected: PASS — 12 tests pass (or skipped if jsonschema not installed; install it and re-run).

- [ ] **Step 6.6: Commit**

```bash
git add src/afteragent/llm/schemas.py tests/test_llm_schemas.py pyproject.toml
git commit -m "$(cat <<'EOF'
Add FINDINGS_SCHEMA and INTERVENTIONS_SCHEMA

JSON schemas for the structured output contract between the LLM
and the enhancer. FINDINGS_SCHEMA includes the origin enum
(confirmed_rule/rejected_rule/novel) that drives merge logic.
INTERVENTIONS_SCHEMA reuses the existing intervention type and
target vocabulary from workflow.py so LLM-authored entries plug
into export_interventions with no other changes.

Exported VALID_* tuples as the single source of truth for enum
values, matched against the embedded schema enums in tests.

Also adds jsonschema as a dev dependency for schema-validation tests.

Sub-project 2 task 6/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `DiagnosisContext` dataclass and loader

**Files:**
- Create: `src/afteragent/llm/prompts.py` (partial — dataclass + loader only, builders in Task 8)
- Create: `tests/test_llm_prompts.py` (partial — loader tests only, builder tests in Task 8)

Goal: the `DiagnosisContext` shape + `load_diagnosis_context(store, run_id)` function that assembles the context from the existing store and artifact dir. This is pure data loading — no prompts yet.

- [ ] **Step 7.1: Write the failing test**

Create `tests/test_llm_prompts.py`:

```python
import tempfile
from pathlib import Path

from afteragent.config import resolve_paths
from afteragent.llm.prompts import DiagnosisContext, load_diagnosis_context
from afteragent.models import PatternFinding
from afteragent.store import Store


def _seed_run_with_artifacts(tmp: Path, run_id: str = "run1") -> Store:
    store = Store(resolve_paths(tmp))
    store.create_run(run_id, "python3 -c 'print(1)'", str(tmp), "2026-04-10T12:00:00Z")

    artifact_dir = store.run_artifact_dir(run_id)
    (artifact_dir / "stdout.log").write_text("first line\nsecond line\nthird line\n")
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

    store.finish_run(run_id, "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")
    return store


def test_load_diagnosis_context_returns_run_metadata(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    assert ctx.run.id == "run1"
    assert ctx.run.command == "python3 -c 'print(1)'"
    assert ctx.run.status == "passed"


def test_load_diagnosis_context_includes_stdout_head_and_tail(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    # 200 lines of stdout.
    stdout_lines = [f"line {i}" for i in range(200)]
    (artifact_dir / "stdout.log").write_text("\n".join(stdout_lines) + "\n")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    ctx = load_diagnosis_context(store, "run1")

    # head = first 50 lines, tail = last 50 lines, each joined on newlines.
    assert "line 0" in ctx.stdout_head
    assert "line 49" in ctx.stdout_head
    assert "line 149" in ctx.stdout_tail
    assert "line 199" in ctx.stdout_tail
    # line 100 is in the middle and should be in neither head nor tail.
    assert "line 100" not in ctx.stdout_head
    assert "line 100" not in ctx.stdout_tail


def test_load_diagnosis_context_caps_head_and_tail_char_budget(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    # 50 lines of 500 chars each = 25 KB, exceeds 5000-char head cap.
    stdout_lines = ["x" * 500 for _ in range(50)]
    (artifact_dir / "stdout.log").write_text("\n".join(stdout_lines) + "\n")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    ctx = load_diagnosis_context(store, "run1")
    assert len(ctx.stdout_head) <= 5000
    assert len(ctx.stdout_tail) <= 5000


def test_load_diagnosis_context_includes_diff_and_changed_files(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    assert "diff --git a/foo.py" in ctx.diff_text
    assert "foo.py" in ctx.changed_files


def test_load_diagnosis_context_truncates_massive_diff(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    # 50 KB diff, exceeds 20 KB cap.
    (artifact_dir / "git_diff_after.patch").write_text(
        "diff --git a/x.py b/x.py\n" + ("x" * 50_000) + "\n"
    )
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    ctx = load_diagnosis_context(store, "run1")
    assert len(ctx.diff_text) <= 21_000  # 20k + truncation marker
    assert "[diff truncated" in ctx.diff_text


def test_load_diagnosis_context_includes_rule_findings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    store.replace_diagnosis(
        "run1",
        [{
            "run_id": "run1",
            "code": "seed_finding",
            "title": "Seeded for test",
            "severity": "medium",
            "summary": "x",
            "evidence_json": "[]",
        }],
        [],
    )

    ctx = load_diagnosis_context(store, "run1")
    rule_codes = [f.code for f in ctx.rule_findings]
    assert "seed_finding" in rule_codes


def test_load_diagnosis_context_includes_transcript_events(tmp_path):
    from afteragent.transcripts import (
        KIND_FILE_READ,
        SOURCE_CLAUDE_CODE_JSONL,
        TranscriptEvent,
    )

    store = _seed_run_with_artifacts(tmp_path)
    store.add_transcript_events(
        "run1",
        [
            TranscriptEvent(
                run_id="run1",
                sequence=0,
                kind=KIND_FILE_READ,
                tool_name="Read",
                target="/repo/foo.py",
                source=SOURCE_CLAUDE_CODE_JSONL,
                raw_ref="line:10",
                timestamp="2026-04-10T12:00:01Z",
            ),
        ],
    )

    ctx = load_diagnosis_context(store, "run1")
    assert len(ctx.transcript_events) == 1
    assert ctx.transcript_events[0].kind == "file_read"


def test_load_diagnosis_context_github_summary_missing_is_none(tmp_path):
    # No github_context.json artifact → summary is None.
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    assert ctx.github_summary is None


def test_load_diagnosis_context_rejects_unknown_run(tmp_path):
    import pytest as _pytest

    store = Store(resolve_paths(tmp_path))
    with _pytest.raises(ValueError, match="Run not found"):
        load_diagnosis_context(store, "does-not-exist")
```

- [ ] **Step 7.2: Run to verify it fails**

Run: `pytest tests/test_llm_prompts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.prompts'`.

- [ ] **Step 7.3: Implement `prompts.py` (dataclass + loader only)**

Create `src/afteragent/llm/prompts.py`:

```python
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import PatternFinding, RunRecord, TranscriptEventRow
from ..store import Store

STDOUT_HEAD_LINES = 50
STDOUT_TAIL_LINES = 50
STDOUT_HEAD_CHAR_CAP = 5000
STDOUT_TAIL_CHAR_CAP = 5000
STDERR_HEAD_LINES = 30
STDERR_TAIL_LINES = 30
STDERR_HEAD_CHAR_CAP = 3000
STDERR_TAIL_CHAR_CAP = 3000
DIFF_CHAR_CAP = 20_000


@dataclass(slots=True)
class GithubSummary:
    repo: str | None
    pr_number: int | None
    failing_checks: list[dict]
    unresolved_review_threads: list[dict]
    ci_log_excerpts: list[str]


@dataclass(slots=True)
class RelatedRunSummary:
    run_id: str
    status: str
    exit_code: int | None
    changed_files: list[str]
    rule_finding_codes: list[str]


@dataclass(slots=True)
class DiagnosisContext:
    run: RunRecord
    rule_findings: list[PatternFinding]
    transcript_events: list[TranscriptEventRow]
    stdout_head: str
    stdout_tail: str
    stderr_head: str
    stderr_tail: str
    diff_text: str
    changed_files: list[str]
    github_summary: GithubSummary | None
    related_runs: list[RelatedRunSummary] = field(default_factory=list)


def load_diagnosis_context(store: Store, run_id: str) -> DiagnosisContext:
    """Assemble all the per-run signals the LLM prompt needs."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")

    artifact_dir = store.run_artifact_dir(run_id)
    stdout = _read_text(artifact_dir / "stdout.log")
    stderr = _read_text(artifact_dir / "stderr.log")
    diff_after = _read_text(artifact_dir / "git_diff_after.patch")

    stdout_head, stdout_tail = _head_and_tail(
        stdout, STDOUT_HEAD_LINES, STDOUT_TAIL_LINES,
        STDOUT_HEAD_CHAR_CAP, STDOUT_TAIL_CHAR_CAP,
    )
    stderr_head, stderr_tail = _head_and_tail(
        stderr, STDERR_HEAD_LINES, STDERR_TAIL_LINES,
        STDERR_HEAD_CHAR_CAP, STDERR_TAIL_CHAR_CAP,
    )
    diff_text = _cap_diff(diff_after)
    changed_files = _extract_changed_files(diff_after)

    rule_findings = _load_rule_findings(store, run_id)
    transcript_events = store.get_transcript_events(run_id)
    github_summary = _load_github_summary(artifact_dir)

    return DiagnosisContext(
        run=run,
        rule_findings=rule_findings,
        transcript_events=transcript_events,
        stdout_head=stdout_head,
        stdout_tail=stdout_tail,
        stderr_head=stderr_head,
        stderr_tail=stderr_tail,
        diff_text=diff_text,
        changed_files=changed_files,
        github_summary=github_summary,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _head_and_tail(
    text: str,
    head_lines: int,
    tail_lines: int,
    head_char_cap: int,
    tail_char_cap: int,
) -> tuple[str, str]:
    lines = text.splitlines()
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:]) if len(lines) > head_lines else ""
    if len(head) > head_char_cap:
        head = head[: head_char_cap - 1] + "…"
    if len(tail) > tail_char_cap:
        tail = tail[: tail_char_cap - 1] + "…"
    return head, tail


def _cap_diff(diff: str) -> str:
    if len(diff) <= DIFF_CHAR_CAP:
        return diff
    head = diff[:DIFF_CHAR_CAP]
    return f"{head}\n\n[diff truncated after {DIFF_CHAR_CAP} chars]"


_DIFF_GIT_LINE = re.compile(r"^diff --git a/(?P<path>[^ ]+) b/", re.MULTILINE)


def _extract_changed_files(diff: str) -> list[str]:
    return sorted(set(m.group("path") for m in _DIFF_GIT_LINE.finditer(diff)))


def _load_rule_findings(store: Store, run_id: str) -> list[PatternFinding]:
    rows = store.get_diagnoses(run_id)
    findings: list[PatternFinding] = []
    for row in rows:
        if row["source"] != "rule":
            continue
        evidence = []
        try:
            evidence = json.loads(row["evidence_json"])
            if not isinstance(evidence, list):
                evidence = []
        except (TypeError, ValueError):
            evidence = []
        findings.append(
            PatternFinding(
                code=row["code"],
                title=row["title"],
                severity=row["severity"],
                summary=row["summary"],
                evidence=[str(e) for e in evidence],
            )
        )
    return findings


def _load_github_summary(artifact_dir: Path) -> GithubSummary | None:
    gh_path = artifact_dir / "github_context.json"
    if not gh_path.exists():
        return None
    try:
        data = json.loads(gh_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return GithubSummary(
        repo=data.get("repo"),
        pr_number=data.get("pr_number"),
        failing_checks=[
            c for c in data.get("checks", []) if (c.get("bucket") or "").lower() == "fail"
        ],
        unresolved_review_threads=[
            t for t in data.get("review_threads", []) if not t.get("is_resolved")
        ],
        ci_log_excerpts=[
            line
            for ci_run in data.get("ci_runs", [])
            for line in (ci_run.get("failed_log_excerpt") or [])[:5]
        ],
    )
```

- [ ] **Step 7.4: Run tests**

Run: `pytest tests/test_llm_prompts.py -v`
Expected: PASS — 9 tests pass.

- [ ] **Step 7.5: Commit**

```bash
git add src/afteragent/llm/prompts.py tests/test_llm_prompts.py
git commit -m "$(cat <<'EOF'
Add DiagnosisContext dataclass and load_diagnosis_context

Assembles per-run signals the LLM prompt will need: run metadata,
rule-based findings (source='rule' only), transcript events (from
sub-project 1's table), stdout/stderr head+tail with char caps,
diff text with 20 KB cap and truncation marker, changed-files list
parsed from the diff, and a GitHub summary pulled from
github_context.json if the artifact exists.

Pure data loading — no prompt composition or LLM calls yet.

Sub-project 2 task 7/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Prompt builders (findings + interventions)

**Files:**
- Modify: `src/afteragent/llm/prompts.py` (add `build_diagnosis_prompt` and `build_interventions_prompt`)
- Modify: `tests/test_llm_prompts.py` (add builder tests)

Goal: the two prompt builders that produce `(system, user)` string pairs from a `DiagnosisContext`. Enforces the ~25k input token ceiling.

- [ ] **Step 8.1: Write the failing test**

Append to `tests/test_llm_prompts.py`:

```python
from afteragent.llm.prompts import (
    build_diagnosis_prompt,
    build_interventions_prompt,
    estimate_tokens,
    MergedFinding,
)


def test_estimate_tokens_is_proportional_to_character_count():
    # Rough heuristic: ~4 chars per token. A 400-char string is ~100 tokens.
    assert 80 <= estimate_tokens("x" * 400) <= 120


def test_build_diagnosis_prompt_returns_system_and_user_strings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    system, user = build_diagnosis_prompt(ctx)

    assert isinstance(system, str) and isinstance(user, str)
    assert "diagnostician" in system.lower()
    # User prompt should reference the command or run id.
    assert ctx.run.id in user or ctx.run.command in user


def test_build_diagnosis_prompt_includes_rule_findings_section_when_present(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    store.replace_diagnosis(
        "run1",
        [{
            "run_id": "run1",
            "code": "seed_finding",
            "title": "Seeded for test",
            "severity": "medium",
            "summary": "a rule was confused",
            "evidence_json": '["hint1", "hint2"]',
        }],
        [],
    )
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "seed_finding" in user
    assert "Seeded for test" in user


def test_build_diagnosis_prompt_omits_rule_findings_section_when_empty(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "## Rule-based findings" not in user or "(none)" in user


def test_build_diagnosis_prompt_includes_transcript_events_when_present(tmp_path):
    from afteragent.transcripts import (
        KIND_FILE_READ,
        SOURCE_CLAUDE_CODE_JSONL,
        TranscriptEvent,
    )

    store = _seed_run_with_artifacts(tmp_path)
    store.add_transcript_events("run1", [
        TranscriptEvent(
            run_id="run1",
            sequence=0,
            kind=KIND_FILE_READ,
            tool_name="Read",
            target="/repo/foo.py",
            source=SOURCE_CLAUDE_CODE_JSONL,
            raw_ref="line:10",
            timestamp="2026-04-10T12:00:01Z",
        ),
    ])
    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)
    assert "/repo/foo.py" in user
    assert "file_read" in user


def test_build_diagnosis_prompt_respects_token_budget(tmp_path):
    store = Store(resolve_paths(tmp_path))
    store.create_run("run1", "cmd", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("run1")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("run1", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    # Write 500 transcript events — enough to push us past the cap if not trimmed.
    from afteragent.transcripts import KIND_BASH_COMMAND, SOURCE_CLAUDE_CODE_JSONL, TranscriptEvent
    events = [
        TranscriptEvent(
            run_id="run1",
            sequence=i,
            kind=KIND_BASH_COMMAND,
            tool_name="Bash",
            target=f"some-long-command-with-lots-of-context-{i} " + ("x" * 100),
            source=SOURCE_CLAUDE_CODE_JSONL,
            raw_ref=f"line:{i}",
            inputs_summary="x" * 150,
            output_excerpt="x" * 200,
            timestamp="2026-04-10T12:00:00Z",
        )
        for i in range(500)
    ]
    store.add_transcript_events("run1", events)

    ctx = load_diagnosis_context(store, "run1")
    _, user = build_diagnosis_prompt(ctx)

    # Max input budget from the spec is ~25k tokens. The builder MUST
    # enforce a hard ceiling, even if that means truncating transcript events.
    assert estimate_tokens(user) <= 25_000


def test_build_interventions_prompt_includes_merged_findings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    merged = [
        MergedFinding(
            code="novel_loop",
            title="Agent in edit loop",
            severity="high",
            summary="edited same file 4 times",
            evidence=["foo.py edited at t=0", "foo.py edited at t=10"],
            source="llm",
        )
    ]

    system, user = build_interventions_prompt(ctx, merged)
    assert "author" in system.lower() and "intervention" in system.lower()
    assert "novel_loop" in user
    assert "Agent in edit loop" in user


def test_build_interventions_prompt_handles_empty_merged_findings(tmp_path):
    store = _seed_run_with_artifacts(tmp_path)
    ctx = load_diagnosis_context(store, "run1")

    system, user = build_interventions_prompt(ctx, [])
    # Still returns valid prompts even when there are no findings.
    assert isinstance(system, str) and isinstance(user, str)
    assert len(system) > 0 and len(user) > 0
```

- [ ] **Step 8.2: Run to verify it fails**

Run: `pytest tests/test_llm_prompts.py -v -k "build or estimate_tokens"`
Expected: FAIL — `ImportError` on `build_diagnosis_prompt`, `build_interventions_prompt`, `estimate_tokens`, `MergedFinding`.

- [ ] **Step 8.3: Implement the builders**

Append to `src/afteragent/llm/prompts.py`:

```python
from dataclasses import asdict


# Hard ceiling on prompt size. Spec budget is ~8-15k typical, ~25k absolute max.
MAX_INPUT_TOKENS = 25_000


@dataclass(slots=True)
class MergedFinding:
    """A finding after LLM merge against rule-based findings. Used by the
    interventions prompt builder and persisted by the enhancer.
    """
    code: str
    title: str
    severity: str
    summary: str
    evidence: list[str]
    source: str  # "rule" | "llm"


def estimate_tokens(text: str) -> int:
    """Rough heuristic: ~4 characters per token.

    Good enough for budget enforcement; the actual tokenizer varies by model
    and we don't want to import model-specific tokenizers just for this.
    """
    return len(text) // 4


_DIAGNOSIS_SYSTEM_PROMPT = """You are a failure-pattern diagnostician for AI coding agent runs. You will be given a single run's full context (the agent's actions, the code changes, the test/CI results, and the pull request review state) plus a list of rule-based findings from a cheap regex detector. Your job:

1. For each rule-based finding, decide whether it applies to THIS specific run. Mark it confirmed_rule (yes, keep it with personalized summary/evidence that cites specifics from this run), rejected_rule (no, this is a false positive — explain why in the summary), or ignore it entirely if you have no opinion.

2. Identify novel failure patterns the rules missed. Common patterns worth naming: agent edited files unrelated to the failure, agent ran tests targeting the wrong file, agent ignored the most specific error message, agent got stuck in a read-edit loop on one file, agent's plan and actions diverged, agent bypassed a review comment about the exact file it edited.

3. Be specific. Cite file paths, test names, error messages, and tool call sequences from the context. Generic findings like "the agent was confused" are not useful.

4. Limit severity=high to findings that, if unaddressed, will cause the next run to fail the same way. Use medium for concerns that probably matter and low for observations worth noting.

5. Return at most 12 findings total. Quality over quantity. Prefer one specific novel finding over three vague confirmed_rule entries.

Output via the `report_findings` tool."""


_INTERVENTIONS_SYSTEM_PROMPT = """You are authoring corrective instructions that will be added to an AI coding agent's instruction file (AGENTS.md / CLAUDE.md), injected into its next task prompt, or written as a runner policy. You will be given a single run's context plus the merged set of confirmed findings from a prior diagnosis pass.

For each relevant finding, author one or more interventions in the existing vocabulary:
- instruction_patch — durable rule to add to the agent's repo instructions
- prompt_patch — text to prepend to the next task prompt
- context_injection_rule — runner-side rule about what context to feed the agent
- runtime_guardrail — policy the runner enforces at tool-call time
- tool_policy_rule — rule about which tools to allow/deny/prefer

Rules:
1. Name specific files, tests, and review comments from the context. Generic advice ("read the failing test first") is less useful than specific advice ("before editing, read tests/test_foo.py::test_add and reconcile with the unresolved review comment at src/arithmetic.py:42").

2. Interventions should be preventative — the next run reading them should naturally avoid the failure.

3. Prefer instruction_patch for durable patterns ("our repo uses X"), prompt_patch for one-off task framing, and runtime_guardrail only when the runner has real control over tool invocations.

4. Each intervention's content field is a plain-text block the agent will read verbatim. Write it in second person, imperative voice.

5. Return at most 10 interventions total. Set related_finding_codes to the finding codes each intervention addresses.

Output via the `author_interventions` tool."""


def build_diagnosis_prompt(context: DiagnosisContext) -> tuple[str, str]:
    """Build (system, user) strings for the findings call."""
    user = _build_base_context_block(context, include_findings_header="Rule-based findings")
    user = _enforce_token_budget(user, context)
    return (_DIAGNOSIS_SYSTEM_PROMPT, user)


def build_interventions_prompt(
    context: DiagnosisContext,
    merged_findings: list[MergedFinding],
) -> tuple[str, str]:
    """Build (system, user) strings for the interventions call."""
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

    user = f"{findings_section}\n\n{base}"
    user = _enforce_token_budget(user, context)
    return (_INTERVENTIONS_SYSTEM_PROMPT, user)


def _build_base_context_block(
    context: DiagnosisContext,
    include_findings_header: str | None,
) -> str:
    """The context sections shared between both prompts."""
    sections: list[str] = []

    # Run metadata.
    sections.append(
        f"## Run metadata\n"
        f"id: {context.run.id}\n"
        f"command: {context.run.command}\n"
        f"status: {context.run.status} (exit code {context.run.exit_code})\n"
        f"duration_ms: {context.run.duration_ms}\n"
        f"cwd: {context.run.cwd}\n"
        f"summary: {context.run.summary or '(none)'}\n"
    )

    # Rule findings (only included in the diagnosis prompt).
    if include_findings_header is not None:
        if context.rule_findings:
            findings_json = json.dumps(
                [
                    {
                        "code": f.code,
                        "title": f.title,
                        "severity": f.severity,
                        "summary": f.summary,
                        "evidence": f.evidence,
                    }
                    for f in context.rule_findings
                ],
                indent=2,
            )
            sections.append(f"## {include_findings_header}\n\n{findings_json}")
        else:
            sections.append(f"## {include_findings_header}\n\n(none)")

    # Transcript events (trimmed list — budget enforced below).
    if context.transcript_events:
        events_json = json.dumps(
            [
                {
                    "sequence": e.sequence,
                    "kind": e.kind,
                    "tool_name": e.tool_name,
                    "target": e.target,
                    "inputs_summary": (e.inputs_summary or "")[:150],
                    "output_excerpt": (e.output_excerpt or "")[:200],
                    "status": e.status,
                    "source": e.source,
                }
                for e in context.transcript_events
            ],
            indent=2,
        )
        sections.append(
            f"## Transcript events ({len(context.transcript_events)} total)\n\n{events_json}"
        )
    else:
        sections.append("## Transcript events\n\n(none)")

    # Git diff.
    if context.diff_text.strip():
        sections.append(f"## Git diff\n\n```diff\n{context.diff_text}\n```")
    else:
        sections.append("## Git diff\n\n(empty)")

    # Changed files.
    if context.changed_files:
        sections.append(
            "## Changed files\n\n" + "\n".join(f"- {p}" for p in context.changed_files)
        )
    else:
        sections.append("## Changed files\n\n(none)")

    # stdout head + tail.
    if context.stdout_head or context.stdout_tail:
        sections.append(
            f"## stdout (head)\n\n```\n{context.stdout_head}\n```\n\n"
            f"## stdout (tail)\n\n```\n{context.stdout_tail}\n```"
        )

    # stderr head + tail.
    if context.stderr_head or context.stderr_tail:
        sections.append(
            f"## stderr (head)\n\n```\n{context.stderr_head}\n```\n\n"
            f"## stderr (tail)\n\n```\n{context.stderr_tail}\n```"
        )

    # GitHub context.
    if context.github_summary is not None:
        gh = context.github_summary
        sections.append(
            f"## GitHub PR context\n"
            f"repo: {gh.repo}\n"
            f"pr_number: {gh.pr_number}\n"
            f"failing_checks: {json.dumps(gh.failing_checks, indent=2)}\n"
            f"unresolved_review_threads: {json.dumps(gh.unresolved_review_threads, indent=2)}\n"
            f"ci_log_excerpts: {json.dumps(gh.ci_log_excerpts, indent=2)}"
        )

    return "\n\n".join(sections)


def _enforce_token_budget(user: str, context: DiagnosisContext) -> str:
    """Trim the user prompt to fit under MAX_INPUT_TOKENS.

    Strategy: if over budget, trim the transcript events section first
    (they're usually the biggest), then clip stdout/stderr tails further,
    then clip the diff more aggressively. Final fallback is a hard
    character cap with a visible truncation marker.
    """
    if estimate_tokens(user) <= MAX_INPUT_TOKENS:
        return user

    # Attempt 1: trim transcript events to the first 50 + last 50.
    if len(context.transcript_events) > 100:
        trimmed_events = context.transcript_events[:50] + context.transcript_events[-50:]
        trimmed_ctx = DiagnosisContext(
            run=context.run,
            rule_findings=context.rule_findings,
            transcript_events=trimmed_events,
            stdout_head=context.stdout_head,
            stdout_tail=context.stdout_tail,
            stderr_head=context.stderr_head,
            stderr_tail=context.stderr_tail,
            diff_text=context.diff_text,
            changed_files=context.changed_files,
            github_summary=context.github_summary,
        )
        user = _build_base_context_block(trimmed_ctx, include_findings_header="Rule-based findings")
        if estimate_tokens(user) <= MAX_INPUT_TOKENS:
            return user + "\n\n[transcript events trimmed to first+last 50 of ~{} total]".format(
                len(context.transcript_events)
            )

    # Attempt 2: hard clip the whole prompt.
    max_chars = MAX_INPUT_TOKENS * 4
    if len(user) > max_chars:
        user = user[:max_chars] + "\n\n[prompt truncated at character budget]"
    return user
```

- [ ] **Step 8.4: Run tests**

Run: `pytest tests/test_llm_prompts.py -v`
Expected: PASS — all new tests pass (~8 new + 9 from task 7 = ~17).

- [ ] **Step 8.5: Commit**

```bash
git add src/afteragent/llm/prompts.py tests/test_llm_prompts.py
git commit -m "$(cat <<'EOF'
Add diagnosis and interventions prompt builders

build_diagnosis_prompt and build_interventions_prompt assemble the
budgeted context block (run metadata, rule findings, transcript
events, diff, stdout/stderr head+tail, GitHub summary) into
(system, user) string pairs.

_enforce_token_budget trims transcript events to first+last 50 when
the budget overruns, with a final hard character clip as a fallback.
estimate_tokens uses a simple ~4-chars-per-token heuristic — good
enough for budget enforcement.

MergedFinding dataclass defined here since both the interventions
prompt builder and the enhancer need it.

Sub-project 2 task 8/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Pure merge logic

**Files:**
- Create: `src/afteragent/llm/merge.py`
- Create: `tests/test_llm_merge.py`

Goal: `merge_findings(rule_findings, llm_findings)` as a pure function. Tests cover every branch of the confirm/reject/novel semantics independently of any LLM call.

- [ ] **Step 9.1: Write the failing test**

Create `tests/test_llm_merge.py`:

```python
from afteragent.llm.merge import merge_findings
from afteragent.llm.prompts import MergedFinding
from afteragent.models import PatternFinding


def _rule(code: str, title: str = "Rule title", summary: str = "Rule summary") -> PatternFinding:
    return PatternFinding(
        code=code,
        title=title,
        severity="medium",
        summary=summary,
        evidence=["rule_evidence_1"],
    )


def _llm(code: str, origin: str, rule_code_ref: str | None = None, **overrides) -> dict:
    return {
        "code": code,
        "title": overrides.get("title", "LLM title"),
        "severity": overrides.get("severity", "medium"),
        "summary": overrides.get("summary", "LLM summary"),
        "evidence": overrides.get("evidence", ["llm_evidence"]),
        "origin": origin,
        "rule_code_ref": rule_code_ref,
    }


def test_confirmed_rule_replaces_summary_and_evidence_with_llm_version():
    rules = [_rule("rule_a", summary="original rule summary")]
    llm = [_llm("rule_a", origin="confirmed_rule", rule_code_ref="rule_a",
                 summary="LLM-personalized summary naming src/foo.py:42",
                 evidence=["cited tests/test_foo.py", "cited src/foo.py"])]

    merged = merge_findings(rules, llm)
    assert len(merged) == 1
    assert merged[0].code == "rule_a"
    assert merged[0].source == "llm"  # becomes LLM-sourced once personalized
    assert "src/foo.py:42" in merged[0].summary
    assert "cited tests/test_foo.py" in merged[0].evidence


def test_rejected_rule_removes_rule_from_merged_list():
    rules = [_rule("false_positive"), _rule("keep_me")]
    llm = [_llm("false_positive", origin="rejected_rule", rule_code_ref="false_positive",
                 summary="this rule doesn't apply because X")]

    merged = merge_findings(rules, llm)
    codes = [m.code for m in merged]
    # false_positive is gone, keep_me stays with source='rule'.
    assert "false_positive" not in codes
    assert "keep_me" in codes
    keep = next(m for m in merged if m.code == "keep_me")
    assert keep.source == "rule"


def test_novel_findings_are_added_as_new_entries_with_llm_source():
    rules = [_rule("rule_a")]
    llm = [_llm("novel_stuck_loop", origin="novel", rule_code_ref=None,
                 title="Agent stuck in loop",
                 summary="Agent edited foo.py 4 times",
                 evidence=["edit 1", "edit 2"])]

    merged = merge_findings(rules, llm)
    codes = [m.code for m in merged]
    assert "rule_a" in codes
    assert "novel_stuck_loop" in codes
    novel = next(m for m in merged if m.code == "novel_stuck_loop")
    assert novel.source == "llm"
    assert novel.title == "Agent stuck in loop"


def test_rule_findings_llm_did_not_address_stay_with_rule_source():
    rules = [_rule("rule_a"), _rule("rule_b")]
    llm = []  # LLM said nothing

    merged = merge_findings(rules, llm)
    assert len(merged) == 2
    assert all(m.source == "rule" for m in merged)
    codes = sorted(m.code for m in merged)
    assert codes == ["rule_a", "rule_b"]


def test_mixed_confirm_reject_novel_and_untouched_rules():
    rules = [
        _rule("confirmed_one"),
        _rule("rejected_one"),
        _rule("untouched_one"),
    ]
    llm = [
        _llm("confirmed_one", origin="confirmed_rule", rule_code_ref="confirmed_one",
             summary="confirmed and personalized"),
        _llm("rejected_one", origin="rejected_rule", rule_code_ref="rejected_one",
             summary="false positive"),
        _llm("brand_new", origin="novel", rule_code_ref=None,
             title="Novel thing", summary="new summary"),
    ]

    merged = merge_findings(rules, llm)
    codes = sorted(m.code for m in merged)
    assert codes == ["brand_new", "confirmed_one", "untouched_one"]

    by_code = {m.code: m for m in merged}
    assert by_code["confirmed_one"].source == "llm"
    assert "confirmed and personalized" in by_code["confirmed_one"].summary
    assert by_code["untouched_one"].source == "rule"
    assert by_code["brand_new"].source == "llm"


def test_confirmed_rule_without_rule_code_ref_is_treated_as_novel():
    """If the LLM says 'confirmed_rule' but doesn't provide rule_code_ref,
    we can't find the rule to confirm — treat it as novel rather than drop it."""
    rules = [_rule("rule_a")]
    llm = [_llm("something", origin="confirmed_rule", rule_code_ref=None,
                 summary="I'm confirming something but I don't know what")]

    merged = merge_findings(rules, llm)
    codes = sorted(m.code for m in merged)
    assert codes == ["rule_a", "something"]
    sources = {m.code: m.source for m in merged}
    assert sources["something"] == "llm"
    assert sources["rule_a"] == "rule"


def test_rejected_rule_with_unknown_rule_code_ref_is_ignored():
    """If the LLM tries to reject a rule that doesn't exist, ignore the entry."""
    rules = [_rule("rule_a")]
    llm = [_llm("wrong", origin="rejected_rule", rule_code_ref="nonexistent",
                 summary="rejecting rule that isn't there")]

    merged = merge_findings(rules, llm)
    codes = sorted(m.code for m in merged)
    # rule_a still present, the bogus rejection does nothing.
    assert codes == ["rule_a"]
    assert merged[0].source == "rule"


def test_duplicate_novel_codes_are_both_kept():
    """If the LLM emits two novel findings with the same code (unusual but
    allowed by the schema), keep both."""
    rules = []
    llm = [
        _llm("dup", origin="novel", rule_code_ref=None, title="First"),
        _llm("dup", origin="novel", rule_code_ref=None, title="Second"),
    ]

    merged = merge_findings(rules, llm)
    assert len(merged) == 2
    assert all(m.code == "dup" for m in merged)
    titles = [m.title for m in merged]
    assert "First" in titles and "Second" in titles
```

- [ ] **Step 9.2: Run to verify it fails**

Run: `pytest tests/test_llm_merge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.merge'`.

- [ ] **Step 9.3: Implement `merge.py`**

Create `src/afteragent/llm/merge.py`:

```python
from __future__ import annotations

from ..models import PatternFinding
from .prompts import MergedFinding


def merge_findings(
    rule_findings: list[PatternFinding],
    llm_findings: list[dict],
) -> list[MergedFinding]:
    """Merge rule-based findings with LLM findings using the origin field.

    For each LLM finding:
      - origin=confirmed_rule with a valid rule_code_ref: overwrite the matching
        rule's summary/evidence with the LLM version. Source becomes "llm".
      - origin=rejected_rule with a valid rule_code_ref: remove the matching
        rule from the merged list.
      - origin=novel (or confirmed_rule with no/invalid rule_code_ref): add as
        a new entry with source="llm". (We're lenient about the fallback case
        because losing a finding is worse than slightly miscategorizing it.)
      - origin=rejected_rule with an invalid rule_code_ref: silently ignore.

    Rule findings the LLM did not address are preserved with source="rule".
    """
    rules_by_code: dict[str, PatternFinding] = {r.code: r for r in rule_findings}
    dropped_rule_codes: set[str] = set()
    overrides: dict[str, MergedFinding] = {}
    novel: list[MergedFinding] = []

    for entry in llm_findings:
        origin = entry.get("origin")
        rule_code_ref = entry.get("rule_code_ref")

        if origin == "rejected_rule":
            if rule_code_ref and rule_code_ref in rules_by_code:
                dropped_rule_codes.add(rule_code_ref)
            # Ignore rejections of unknown rules.
            continue

        if origin == "confirmed_rule" and rule_code_ref and rule_code_ref in rules_by_code:
            overrides[rule_code_ref] = MergedFinding(
                code=rule_code_ref,
                title=entry.get("title") or rules_by_code[rule_code_ref].title,
                severity=entry.get("severity") or rules_by_code[rule_code_ref].severity,
                summary=entry.get("summary") or rules_by_code[rule_code_ref].summary,
                evidence=list(entry.get("evidence") or rules_by_code[rule_code_ref].evidence),
                source="llm",
            )
            continue

        # Novel (or malformed confirmed_rule): treat as a new LLM finding.
        novel.append(
            MergedFinding(
                code=entry.get("code", "unknown"),
                title=entry.get("title", ""),
                severity=entry.get("severity", "low"),
                summary=entry.get("summary", ""),
                evidence=list(entry.get("evidence") or []),
                source="llm",
            )
        )

    merged: list[MergedFinding] = []
    for rule in rule_findings:
        if rule.code in dropped_rule_codes:
            continue
        if rule.code in overrides:
            merged.append(overrides[rule.code])
            continue
        merged.append(
            MergedFinding(
                code=rule.code,
                title=rule.title,
                severity=rule.severity,
                summary=rule.summary,
                evidence=list(rule.evidence),
                source="rule",
            )
        )
    merged.extend(novel)
    return merged
```

- [ ] **Step 9.4: Run tests**

Run: `pytest tests/test_llm_merge.py -v`
Expected: PASS — 8 tests pass.

- [ ] **Step 9.5: Commit**

```bash
git add src/afteragent/llm/merge.py tests/test_llm_merge.py
git commit -m "$(cat <<'EOF'
Add pure merge logic for LLM vs rule-based findings

merge_findings(rule_findings, llm_findings) implements the confirm/
reject/novel semantics as a pure function:

- confirmed_rule with valid rule_code_ref: rule's title/summary/
  evidence overwritten with LLM version, source becomes 'llm'
- rejected_rule with valid rule_code_ref: rule removed from merged
  list
- novel (or confirmed_rule with missing/invalid ref): added as new
  LLM-sourced entry
- rejected_rule with invalid rule_code_ref: silently ignored
- rule findings the LLM didn't address: preserved with source='rule'

Separate module + pure function so the enhancer tests can focus on
orchestration rather than merge edge cases.

Sub-project 2 task 9/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `enhance_diagnosis_with_llm` orchestration

**Files:**
- Create: `src/afteragent/llm/enhancer.py`
- Create: `tests/test_llm_enhancer.py`

Goal: the orchestrator that ties config + client + context + prompt + merge + store together. Tests use a stub `LLMClient` that returns canned `StructuredResponse` objects.

- [ ] **Step 10.1: Write the failing test**

Create `tests/test_llm_enhancer.py`:

```python
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from afteragent.config import resolve_paths
from afteragent.llm.client import StructuredResponse
from afteragent.llm.config import LLMConfig
from afteragent.llm.enhancer import EnhanceResult, enhance_diagnosis_with_llm
from afteragent.store import Store


@dataclass
class StubClient:
    """Records every call_structured invocation and returns canned responses
    keyed by tool_name."""
    responses: dict[str, StructuredResponse | Exception] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    name: str = "stub"
    model: str = "stub-model"

    def call_structured(self, system, user, schema, tool_name):
        self.calls.append({
            "system": system,
            "user": user,
            "schema": schema,
            "tool_name": tool_name,
        })
        result = self.responses.get(tool_name)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError(f"No stub response configured for {tool_name}")
        return result


def _make_store(tmp: Path) -> Store:
    return Store(resolve_paths(tmp))


def _seed_minimal_run(store: Store, run_id: str = "run1") -> None:
    store.create_run(run_id, "python3 -c 'print(1)'", "/tmp", "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir(run_id)
    (artifact_dir / "stdout.log").write_text("hello\nworld\n")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run(run_id, "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")


def _success_findings_response(findings: list[dict]) -> StructuredResponse:
    return StructuredResponse(
        data={"findings": findings},
        input_tokens=1000,
        output_tokens=100,
        model="stub-model",
        provider="stub",
        duration_ms=500,
        raw_response_excerpt='{"findings": [...]}',
    )


def _success_interventions_response(interventions: list[dict]) -> StructuredResponse:
    return StructuredResponse(
        data={"interventions": interventions},
        input_tokens=1200,
        output_tokens=150,
        model="stub-model",
        provider="stub",
        duration_ms=600,
        raw_response_excerpt='{"interventions": [...]}',
    )


def _make_config() -> LLMConfig:
    return LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-ant-test",
        base_url=None,
    )


def test_enhance_with_novel_finding_and_intervention(tmp_path):
    store = _make_store(tmp_path)
    _seed_minimal_run(store)

    client = StubClient(responses={
        "report_findings": _success_findings_response([{
            "code": "novel_stuck_loop",
            "title": "Agent in read-edit loop",
            "severity": "high",
            "summary": "Agent edited foo.py 4 times without reading test output",
            "evidence": ["edit 1", "edit 2"],
            "origin": "novel",
            "rule_code_ref": None,
        }]),
        "author_interventions": _success_interventions_response([{
            "type": "prompt_patch",
            "title": "Read test output between edits",
            "target": "task_prompt",
            "content": "After each edit, run the failing test and read its output before editing again.",
            "scope": "pr",
            "related_finding_codes": ["novel_stuck_loop"],
        }]),
    })

    result = enhance_diagnosis_with_llm(store, "run1", client, _make_config())
    assert isinstance(result, EnhanceResult)
    assert result.status == "success"

    # Findings persisted with source=llm.
    diagnoses = store.get_diagnoses("run1")
    assert len(diagnoses) == 1
    assert diagnoses[0]["code"] == "novel_stuck_loop"
    assert diagnoses[0]["source"] == "llm"

    # Intervention persisted with source=llm.
    interventions = store.get_interventions("run1")
    assert len(interventions) == 1
    assert interventions[0]["source"] == "llm"
    assert interventions[0]["type"] == "prompt_patch"

    # Two generation rows recorded (findings + interventions, both success).
    gens = store.get_llm_generations("run1")
    assert len(gens) == 2
    kinds = sorted(g["kind"] for g in gens)
    assert kinds == ["findings", "interventions"]
    assert all(g["status"] == "success" for g in gens)


def test_enhance_confirmed_rule_overwrites_rule_with_llm_version(tmp_path):
    store = _make_store(tmp_path)
    _seed_minimal_run(store)
    # Seed a rule-based finding.
    store.replace_diagnosis(
        "run1",
        [{
            "run_id": "run1",
            "code": "low_overlap",
            "title": "Low overlap",
            "severity": "medium",
            "summary": "original rule summary",
            "evidence_json": '["rule_evidence"]',
        }],
        [],
    )

    client = StubClient(responses={
        "report_findings": _success_findings_response([{
            "code": "low_overlap",
            "title": "Low overlap (personalized)",
            "severity": "high",
            "summary": "LLM-personalized: diff edits src/unrelated.py, failing test is tests/test_foo.py",
            "evidence": ["tests/test_foo.py", "src/unrelated.py"],
            "origin": "confirmed_rule",
            "rule_code_ref": "low_overlap",
        }]),
        "author_interventions": _success_interventions_response([]),
    })

    enhance_diagnosis_with_llm(store, "run1", client, _make_config())

    # After merge: one finding with code=low_overlap, source=llm, personalized
    # summary. The original rule row with source=rule is gone.
    diagnoses = store.get_diagnoses("run1")
    assert len(diagnoses) == 1
    assert diagnoses[0]["code"] == "low_overlap"
    assert diagnoses[0]["source"] == "llm"
    assert "personalized" in diagnoses[0]["summary"]


def test_enhance_rejected_rule_removes_rule_from_merged(tmp_path):
    store = _make_store(tmp_path)
    _seed_minimal_run(store)
    store.replace_diagnosis(
        "run1",
        [
            {
                "run_id": "run1",
                "code": "false_positive",
                "title": "False positive",
                "severity": "medium",
                "summary": "x",
                "evidence_json": "[]",
            },
            {
                "run_id": "run1",
                "code": "keep_me",
                "title": "Keep me",
                "severity": "low",
                "summary": "y",
                "evidence_json": "[]",
            },
        ],
        [],
    )

    client = StubClient(responses={
        "report_findings": _success_findings_response([{
            "code": "false_positive",
            "title": "rejected",
            "severity": "medium",
            "summary": "this doesn't apply here because X",
            "evidence": [],
            "origin": "rejected_rule",
            "rule_code_ref": "false_positive",
        }]),
        "author_interventions": _success_interventions_response([]),
    })

    enhance_diagnosis_with_llm(store, "run1", client, _make_config())

    diagnoses = store.get_diagnoses("run1")
    codes = sorted(d["code"] for d in diagnoses)
    # false_positive gone, keep_me stays with source='rule'.
    assert codes == ["keep_me"]
    assert diagnoses[0]["source"] == "rule"


def test_enhance_findings_call_failure_preserves_rule_findings(tmp_path):
    store = _make_store(tmp_path)
    _seed_minimal_run(store)
    store.replace_diagnosis(
        "run1",
        [{
            "run_id": "run1",
            "code": "preserve_me",
            "title": "Preserve me",
            "severity": "medium",
            "summary": "x",
            "evidence_json": "[]",
        }],
        [],
    )

    client = StubClient(responses={
        "report_findings": RuntimeError("simulated rate limit"),
        "author_interventions": _success_interventions_response([]),
    })

    result = enhance_diagnosis_with_llm(store, "run1", client, _make_config())
    assert result.status == "error"

    # Rule-based finding is still there, untouched.
    diagnoses = store.get_diagnoses("run1")
    assert any(d["code"] == "preserve_me" and d["source"] == "rule" for d in diagnoses)

    # A diagnosis_error finding was NOT added to the rule-based table (we
    # preserve the rule-based state on failure). But a failed generation row
    # is recorded.
    gens = store.get_llm_generations("run1")
    assert len(gens) >= 1
    assert any(g["kind"] == "findings" and g["status"] == "error" for g in gens)


def test_enhance_interventions_call_failure_persists_merged_findings(tmp_path):
    store = _make_store(tmp_path)
    _seed_minimal_run(store)

    client = StubClient(responses={
        "report_findings": _success_findings_response([{
            "code": "novel",
            "title": "novel finding",
            "severity": "medium",
            "summary": "x",
            "evidence": [],
            "origin": "novel",
            "rule_code_ref": None,
        }]),
        "author_interventions": RuntimeError("simulated schema rejection"),
    })

    result = enhance_diagnosis_with_llm(store, "run1", client, _make_config())
    # Overall status is partial/error but findings did land.
    diagnoses = store.get_diagnoses("run1")
    assert any(d["code"] == "novel" and d["source"] == "llm" for d in diagnoses)

    # Two generation rows: findings=success, interventions=error.
    gens = store.get_llm_generations("run1")
    kinds_to_status = {g["kind"]: g["status"] for g in gens}
    assert kinds_to_status.get("findings") == "success"
    assert kinds_to_status.get("interventions") == "error"
```

- [ ] **Step 10.2: Run to verify it fails**

Run: `pytest tests/test_llm_enhancer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'afteragent.llm.enhancer'`.

- [ ] **Step 10.3: Implement `enhancer.py`**

Create `src/afteragent/llm/enhancer.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass

from ..models import now_utc
from ..store import Store
from .client import LLMClient, StructuredResponse
from .config import LLMConfig
from .cost_table import estimate_cost
from .merge import merge_findings
from .prompts import (
    MergedFinding,
    build_diagnosis_prompt,
    build_interventions_prompt,
    load_diagnosis_context,
)
from .schemas import FINDINGS_SCHEMA, INTERVENTIONS_SCHEMA


@dataclass(slots=True)
class EnhanceResult:
    status: str           # "success" | "error" | "partial"
    findings_count: int
    interventions_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    error_messages: list[str]


def enhance_diagnosis_with_llm(
    store: Store,
    run_id: str,
    client: LLMClient,
    config: LLMConfig,
) -> EnhanceResult:
    """Run the two-call LLM enhancement pass: findings, then interventions.

    Never raises. Every failure becomes a recorded llm_generations row with
    status='error' and preserves any successful state from earlier calls.
    """
    errors: list[str] = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    findings_count = 0
    interventions_count = 0

    context = load_diagnosis_context(store, run_id)

    # ----- Findings call -----
    system, user = build_diagnosis_prompt(context)
    merged: list[MergedFinding] | None = None
    try:
        response = client.call_structured(
            system=system,
            user=user,
            schema=FINDINGS_SCHEMA,
            tool_name="report_findings",
        )
        _record_generation(
            store=store,
            run_id=run_id,
            kind="findings",
            response=response,
            status="success",
            error_message=None,
            config=config,
        )
        total_in += response.input_tokens
        total_out += response.output_tokens
        total_cost += estimate_cost(
            response.provider, response.model, response.input_tokens, response.output_tokens
        )

        llm_findings = response.data.get("findings", [])
        merged = merge_findings(context.rule_findings, llm_findings)
        findings_count = len(merged)

    except Exception as exc:
        errors.append(f"findings call failed: {exc}")
        _record_error_generation(
            store=store,
            run_id=run_id,
            kind="findings",
            provider=getattr(client, "name", "unknown"),
            model=getattr(client, "model", "unknown"),
            error_message=str(exc),
            config=config,
        )
        # Rule-based findings are preserved — we don't overwrite them.
        return EnhanceResult(
            status="error",
            findings_count=0,
            interventions_count=0,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_cost_usd=total_cost,
            error_messages=errors,
        )

    # ----- Interventions call -----
    system, user = build_interventions_prompt(context, merged)
    llm_interventions: list[dict] = []
    try:
        response = client.call_structured(
            system=system,
            user=user,
            schema=INTERVENTIONS_SCHEMA,
            tool_name="author_interventions",
        )
        _record_generation(
            store=store,
            run_id=run_id,
            kind="interventions",
            response=response,
            status="success",
            error_message=None,
            config=config,
        )
        total_in += response.input_tokens
        total_out += response.output_tokens
        total_cost += estimate_cost(
            response.provider, response.model, response.input_tokens, response.output_tokens
        )
        llm_interventions = response.data.get("interventions", [])
        interventions_count = len(llm_interventions)
    except Exception as exc:
        errors.append(f"interventions call failed: {exc}")
        _record_error_generation(
            store=store,
            run_id=run_id,
            kind="interventions",
            provider=getattr(client, "name", "unknown"),
            model=getattr(client, "model", "unknown"),
            error_message=str(exc),
            config=config,
        )
        # Fall through: persist merged findings but with no LLM interventions.
        # build_interventions in diagnostics.py will fall back to the
        # hardcoded templates for the merged findings.

    # ----- Persist -----
    store.replace_llm_diagnosis(
        run_id=run_id,
        findings_rows=[
            _merged_finding_to_row(run_id, f) for f in merged if f.source == "llm"
        ],
        interventions_rows=[
            _intervention_dict_to_row(run_id, i) for i in llm_interventions
        ],
    )

    status = "success" if not errors else ("partial" if merged else "error")
    return EnhanceResult(
        status=status,
        findings_count=findings_count,
        interventions_count=interventions_count,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_cost_usd=total_cost,
        error_messages=errors,
    )


def _record_generation(
    store: Store,
    run_id: str,
    kind: str,
    response: StructuredResponse,
    status: str,
    error_message: str | None,
    config: LLMConfig,
) -> None:
    store.record_llm_generation(
        run_id=run_id,
        kind=kind,
        provider=response.provider,
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        duration_ms=response.duration_ms,
        estimated_cost_usd=estimate_cost(
            response.provider, response.model, response.input_tokens, response.output_tokens
        ),
        status=status,
        error_message=error_message,
        created_at=now_utc(),
        raw_response_excerpt=response.raw_response_excerpt,
    )


def _record_error_generation(
    store: Store,
    run_id: str,
    kind: str,
    provider: str,
    model: str,
    error_message: str,
    config: LLMConfig,
) -> None:
    store.record_llm_generation(
        run_id=run_id,
        kind=kind,
        provider=provider,
        model=model,
        input_tokens=0,
        output_tokens=0,
        duration_ms=0,
        estimated_cost_usd=0.0,
        status="error",
        error_message=error_message,
        created_at=now_utc(),
        raw_response_excerpt="",
    )


def _merged_finding_to_row(run_id: str, finding: MergedFinding) -> dict:
    return {
        "run_id": run_id,
        "code": finding.code,
        "title": finding.title,
        "severity": finding.severity,
        "summary": finding.summary,
        "evidence_json": json.dumps(finding.evidence),
        "source": "llm",
    }


def _intervention_dict_to_row(run_id: str, entry: dict) -> dict:
    return {
        "run_id": run_id,
        "type": entry["type"],
        "title": entry["title"],
        "target": entry["target"],
        "content": entry["content"],
        "scope": entry.get("scope", "pr"),
        "source": "llm",
    }
```

- [ ] **Step 10.4: Run tests**

Run: `pytest tests/test_llm_enhancer.py -v`
Expected: PASS — 5 tests pass.

Then run the full suite:
Run: `pytest -v`
Expected: PASS — everything green.

- [ ] **Step 10.5: Commit**

```bash
git add src/afteragent/llm/enhancer.py tests/test_llm_enhancer.py
git commit -m "$(cat <<'EOF'
Add enhance_diagnosis_with_llm orchestrator

Ties config + client + prompt builders + merge + store together into
a single entry point that runs two LLM calls (findings, interventions)
and persists the merged result via replace_llm_diagnosis +
record_llm_generation.

Graceful failure: if the findings call fails, rule-based findings
are preserved and an error generation row is recorded. If the
interventions call fails after successful findings, the merged
findings still land in the store and the interventions generation
is recorded as error (downstream callers fall back to hardcoded
intervention templates).

Sub-project 2 task 10/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: `diagnostics.py` hook for LLM interventions

**Files:**
- Modify: `src/afteragent/diagnostics.py`
- Create: `tests/test_diagnostics_llm_hook.py`

Goal: refactor `build_interventions` to accept an optional `llm_interventions` parameter — when provided, they're used in place of the hardcoded strings. Existing behavior (no LLM) stays identical. Add a small helper `persist_llm_enhanced_diagnosis` for future `workflow.py` integration.

- [ ] **Step 11.1: Write the failing test**

Create `tests/test_diagnostics_llm_hook.py`:

```python
from afteragent.diagnostics import build_interventions
from afteragent.models import Intervention, PatternFinding


def _finding(code: str) -> PatternFinding:
    return PatternFinding(
        code=code,
        title=f"Title for {code}",
        severity="high",
        summary="summary",
        evidence=["e1"],
    )


def test_build_interventions_without_llm_produces_hardcoded_strings():
    """Existing behavior: a finding with a known code produces hardcoded
    intervention templates."""
    findings = [_finding("low_diff_overlap_with_failing_files")]
    result = build_interventions(findings)
    assert len(result) > 0
    # The hardcoded content is the old string we shipped; it should still
    # mention something about mapping edits to failures.
    assert any("failure surface" in i.content.lower() or "failing" in i.content.lower() for i in result)


def test_build_interventions_with_llm_uses_llm_list():
    """When llm_interventions are passed, they replace the hardcoded strings."""
    findings = [_finding("low_diff_overlap_with_failing_files")]
    llm_interventions = [
        Intervention(
            type="prompt_patch",
            title="LLM-authored",
            target="task_prompt",
            content="LLM-written content that names specific files",
            scope="pr",
        )
    ]
    result = build_interventions(findings, llm_interventions=llm_interventions)
    assert len(result) == 1
    assert result[0].title == "LLM-authored"
    assert "specific files" in result[0].content


def test_build_interventions_with_empty_llm_list_falls_back_to_hardcoded():
    """If the LLM returned zero interventions, fall back to the hardcoded
    path — users shouldn't lose intervention coverage on an LLM miss."""
    findings = [_finding("low_diff_overlap_with_failing_files")]
    result = build_interventions(findings, llm_interventions=[])
    # Fall back — hardcoded interventions for this finding code are non-empty.
    assert len(result) > 0


def test_build_interventions_with_none_llm_uses_hardcoded():
    """Explicit None (the default) is the same as omitting the argument."""
    findings = [_finding("low_diff_overlap_with_failing_files")]
    result_none = build_interventions(findings, llm_interventions=None)
    result_default = build_interventions(findings)
    assert len(result_none) == len(result_default)
```

- [ ] **Step 11.2: Run to verify it fails**

Run: `pytest tests/test_diagnostics_llm_hook.py -v`
Expected: FAIL — `build_interventions` does not accept an `llm_interventions` keyword argument yet.

- [ ] **Step 11.3: Modify `build_interventions` in `diagnostics.py`**

Find the existing function signature in `src/afteragent/diagnostics.py`:

```python
def build_interventions(findings: list[PatternFinding]) -> list[Intervention]:
```

Replace with:

```python
def build_interventions(
    findings: list[PatternFinding],
    llm_interventions: list[Intervention] | None = None,
) -> list[Intervention]:
    """Build interventions for a set of findings.

    If llm_interventions is provided and non-empty, use those directly.
    Otherwise fall back to the hardcoded templates keyed by finding code.
    Passing an empty list is treated the same as None (fallback) so that
    callers who got nothing from the LLM still get intervention coverage.
    """
    if llm_interventions:
        return list(llm_interventions)
    # Original hardcoded implementation follows unchanged.
    interventions: list[Intervention] = []
    codes = {finding.code for finding in findings}
    # ... (existing body stays exactly the same)
```

**Do not modify the rest of the existing body** — it's the long `if "..." in codes:` chain. Just change the signature and add the early-return block at the top.

- [ ] **Step 11.4: Run tests**

Run: `pytest tests/test_diagnostics_llm_hook.py tests/test_diagnostics.py -v`
Expected: PASS — new tests pass, existing `test_diagnostics.py` tests still pass (they don't pass the new kwarg, so they hit the default `None` path).

- [ ] **Step 11.5: Commit**

```bash
git add src/afteragent/diagnostics.py tests/test_diagnostics_llm_hook.py
git commit -m "$(cat <<'EOF'
Extend build_interventions with optional llm_interventions parameter

When callers pass llm_interventions (non-empty list), those are used
directly. When None or empty, the function falls back to the existing
hardcoded intervention templates keyed by finding code.

Existing callers that don't pass the new kwarg see no behavior change.

Sub-project 2 task 11/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: CLI — `enhance` subcommand and `--enhance`/`--no-enhance` flags

**Files:**
- Modify: `src/afteragent/cli.py`
- Modify: `tests/test_cli.py`

Goal: add `afteragent enhance <run-id>` subcommand that loads the config, builds the client, runs `enhance_diagnosis_with_llm`, and prints a one-line summary. Add `--enhance` / `--no-enhance` flags to `exec` that override the config's `auto_enhance_on_exec` setting.

- [ ] **Step 12.1: Write the failing test**

Append to `tests/test_cli.py`:

```python
import sys
from unittest.mock import MagicMock, patch

from afteragent.cli import main


def test_enhance_subcommand_parses_and_dispatches(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    # Seed a run so the CLI has something to enhance.
    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())
    store.create_run("test_run_id", "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("test_run_id")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("test_run_id", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    # Unset any real API keys.
    for var in [
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "OLLAMA_BASE_URL", "AFTERAGENT_LLM_PROVIDER",
    ]:
        monkeypatch.delenv(var, raising=False)

    # No config → enhance should exit 1 with a clear message.
    exit_code = main(["enhance", "test_run_id"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "No LLM provider configured" in captured.out or "No LLM provider configured" in captured.err


def test_enhance_subcommand_calls_enhancer_when_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    from afteragent.config import resolve_paths
    from afteragent.store import Store
    store = Store(resolve_paths())
    store.create_run("test_run_id", "echo hi", str(tmp_path), "2026-04-10T12:00:00Z")
    artifact_dir = store.run_artifact_dir("test_run_id")
    (artifact_dir / "stdout.log").write_text("")
    (artifact_dir / "stderr.log").write_text("")
    (artifact_dir / "git_diff_before.patch").write_text("")
    (artifact_dir / "git_diff_after.patch").write_text("")
    store.finish_run("test_run_id", "passed", 0, "2026-04-10T12:00:01Z", 1000, "ok")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    # Mock get_client so we don't need the real anthropic SDK.
    stub_client = MagicMock()
    stub_client.name = "anthropic"
    stub_client.model = "claude-sonnet-4-5"

    # Patch get_client and enhance_diagnosis_with_llm to avoid real calls.
    with patch("afteragent.llm.client.get_client", return_value=stub_client), \
         patch("afteragent.llm.enhancer.enhance_diagnosis_with_llm") as mock_enhance:
        from afteragent.llm.enhancer import EnhanceResult
        mock_enhance.return_value = EnhanceResult(
            status="success",
            findings_count=2,
            interventions_count=1,
            total_input_tokens=1000,
            total_output_tokens=100,
            total_cost_usd=0.005,
            error_messages=[],
        )

        exit_code = main(["enhance", "test_run_id"])

    assert exit_code == 0
    mock_enhance.assert_called_once()
    captured = capsys.readouterr()
    assert "Enhanced run" in captured.out
    assert "test_run_id" in captured.out
    assert "+2 findings" in captured.out or "2 findings" in captured.out
    assert "1 intervention" in captured.out


def test_exec_enhance_flag_present(tmp_path, monkeypatch):
    """The argparse parser accepts --enhance and --no-enhance on exec."""
    from afteragent.cli import build_parser
    parser = build_parser()

    # Should not raise.
    args = parser.parse_args(["exec", "--enhance", "--", "echo", "hi"])
    assert getattr(args, "enhance", None) is True

    args = parser.parse_args(["exec", "--no-enhance", "--", "echo", "hi"])
    assert getattr(args, "enhance", None) is False
```

- [ ] **Step 12.2: Run to verify it fails**

Run: `pytest tests/test_cli.py -v -k "enhance"`
Expected: FAIL — `enhance` subcommand doesn't exist, `--enhance` flag doesn't exist.

- [ ] **Step 12.3: Add subcommand and flags to `cli.py`**

In `src/afteragent/cli.py`, find the `build_parser` function and the `exec_parser` definition. Add `--enhance` and `--no-enhance` as a mutually-exclusive group:

```python
    # Find the existing exec_parser section and add after its existing flags:
    enhance_group = exec_parser.add_mutually_exclusive_group()
    enhance_group.add_argument(
        "--enhance",
        dest="enhance",
        action="store_true",
        default=None,
        help="Force LLM enhancement after the run, overriding config.",
    )
    enhance_group.add_argument(
        "--no-enhance",
        dest="enhance",
        action="store_false",
        default=None,
        help="Skip LLM enhancement for this run, overriding config.",
    )
```

Then add the `enhance` subcommand parser after the existing subparsers:

```python
    enhance_parser = subparsers.add_parser(
        "enhance", help="Run LLM-driven diagnosis enhancement on a captured run"
    )
    enhance_parser.add_argument("run_id", help="Run ID to enhance")
    enhance_parser.add_argument(
        "--llm-provider",
        help="Override LLM provider (anthropic | openai | openrouter | ollama)",
    )
    enhance_parser.add_argument("--llm-model", help="Override LLM model name")
    enhance_parser.add_argument("--llm-base-url", help="Override LLM base URL")
```

In the `main` function, add a dispatch branch for the `enhance` subcommand. Place it alongside the other `if args.command == "..."` blocks:

```python
    if args.command == "enhance":
        from .llm.config import load_config
        from .llm import client as llm_client_module
        from .llm.enhancer import enhance_diagnosis_with_llm

        cli_overrides = {}
        if args.llm_provider:
            cli_overrides["provider"] = args.llm_provider
        if args.llm_model:
            cli_overrides["model"] = args.llm_model
        if args.llm_base_url:
            cli_overrides["base_url"] = args.llm_base_url

        config = load_config(store.paths, cli_overrides=cli_overrides or None)
        if config is None:
            print(
                "No LLM provider configured. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                "OPENROUTER_API_KEY / OLLAMA_BASE_URL, or create .afteragent/config.toml. "
                "See `afteragent enhance --help`."
            )
            return 1

        try:
            client = llm_client_module.get_client(config)
        except ImportError as exc:
            print(f"Cannot instantiate LLM client: {exc}")
            return 1

        result = enhance_diagnosis_with_llm(store, args.run_id, client, config)
        cost_str = f"${result.total_cost_usd:.4f}" if result.total_cost_usd > 0 else "free"
        print(
            f"Enhanced run {args.run_id}: "
            f"+{result.findings_count} findings, "
            f"{result.interventions_count} intervention(s) "
            f"({result.total_input_tokens} in / {result.total_output_tokens} out tokens, "
            f"{cost_str})"
        )
        if result.error_messages:
            for err in result.error_messages:
                print(f"  warning: {err}")
        return 0 if result.status != "error" else 1
```

Also update the `exec` dispatch to honor the `--enhance`/`--no-enhance` flag. Find the existing `if args.command == "exec":` block and after the `run_command(...)` call, add:

```python
        # Decide whether to auto-enhance. Precedence:
        # 1. CLI flag (args.enhance is not None)
        # 2. Config file (auto_enhance_on_exec)
        # 3. Default: no enhancement
        should_enhance: bool | None = getattr(args, "enhance", None)
        if should_enhance is None:
            from .llm.config import load_config
            config = load_config(store.paths)
            should_enhance = bool(config and config.auto_enhance_on_exec)

        if should_enhance:
            from .llm.config import load_config
            from .llm import client as llm_client_module
            from .llm.enhancer import enhance_diagnosis_with_llm

            config = load_config(store.paths)
            if config is None:
                print(
                    "  (enhance requested but no LLM provider configured — skipping)"
                )
            else:
                try:
                    client = llm_client_module.get_client(config)
                    enhance_result = enhance_diagnosis_with_llm(
                        store, run_id, client, config,
                    )
                    cost_str = (
                        f"${enhance_result.total_cost_usd:.4f}"
                        if enhance_result.total_cost_usd > 0
                        else "free"
                    )
                    print(
                        f"  enhanced: +{enhance_result.findings_count} findings, "
                        f"{enhance_result.interventions_count} intervention(s) "
                        f"({cost_str})"
                    )
                except ImportError as exc:
                    print(f"  (LLM enhancement skipped: {exc})")
```

Place this block just before the existing `return int(result["exit_code"])` at the end of the `exec` branch.

- [ ] **Step 12.4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: PASS — new tests pass, existing CLI tests still pass.

Run the full suite:
Run: `pytest -v`
Expected: PASS — everything green.

- [ ] **Step 12.5: Commit**

```bash
git add src/afteragent/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
Add `afteragent enhance` subcommand and --enhance flag on exec

New `afteragent enhance <run-id>` subcommand loads the LLM config,
instantiates the configured client, runs enhance_diagnosis_with_llm,
and prints a one-line summary with token counts and cost estimate.

`afteragent exec` gains --enhance / --no-enhance mutually-exclusive
flags that override the config file's auto_enhance_on_exec setting
on a per-call basis. When the flag is absent, the config value
(default: false) applies.

Precedence for auto-enhance: CLI flag > config file > default off.
When enhancement is requested but no provider is configured, a
one-line warning is printed and the run continues normally.

Sub-project 2 task 12/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: `pyproject.toml` optional dependencies + README

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md` (add Ollama recipe section)
- Modify: `scripts/e2e_matrix.sh` (add llm test block)

Goal: declare the `anthropic`, `openai`, and combined `all` extras. Add a section to the README showing how to dogfood with Ollama locally. Extend the e2e matrix to run the new LLM tests.

- [ ] **Step 13.1: Update `pyproject.toml`**

Find the existing `[project.optional-dependencies]` section (or create one if missing from earlier tasks). Ensure it has all of these:

```toml
[project.optional-dependencies]
anthropic = ["anthropic>=0.40"]
openai = ["openai>=1.50"]
all = ["anthropic>=0.40", "openai>=1.50"]
dev = ["jsonschema>=4.22"]
```

- [ ] **Step 13.2: Update the e2e matrix**

Find the existing `Running transcript ingestion tests...` block in `scripts/e2e_matrix.sh`. After it, append a new block:

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
    tests/test_diagnostics_llm_hook.py
```

- [ ] **Step 13.3: Add an Ollama recipe section to the README**

Find the existing README and append a new section at the end (or somewhere appropriate within the usage section):

```markdown
## LLM-driven diagnosis (sub-project 2)

AfterAgent can optionally run an LLM pass over each captured run to identify failure patterns the rule-based detector missed and to author interventions tailored to that specific run. Four providers are supported: Anthropic, OpenAI, OpenRouter, and Ollama.

### Zero-config: just set an API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
afteragent enhance <run-id>
```

Auto-detect picks `anthropic` + `claude-sonnet-4-5` by default.

### Per-run override

```bash
afteragent exec --enhance -- claude "fix the failing test"
```

Forces LLM enhancement for this run even if `auto_enhance_on_exec = false` in config.

### Auto-enhance every run

Create `.afteragent/config.toml`:

```toml
[llm]
provider = "anthropic"
model = "claude-sonnet-4-5"
auto_enhance_on_exec = true
```

### Ollama dogfood (free, local, offline)

```bash
ollama pull qwen2.5-coder:7b
mkdir -p .afteragent && cat > .afteragent/config.toml <<EOF
[llm]
provider = "ollama"
model = "qwen2.5-coder:7b"
EOF

afteragent exec -- claude "fix the failing test"
afteragent enhance <run-id>
```

No API key required. Runs entirely on your machine. Recommended for local iteration before spending on hosted API tokens.

### Installing provider SDKs

The anthropic and openai SDKs are optional dependencies:

```bash
pip install afteragent[anthropic]       # Anthropic only
pip install afteragent[openai]          # OpenAI / OpenRouter / Ollama
pip install afteragent[all]             # Both SDKs
```
```

- [ ] **Step 13.4: Verify the matrix still passes**

Run: `bash scripts/e2e_matrix.sh`
Expected: all blocks pass, including the new LLM tests block.

- [ ] **Step 13.5: Commit**

```bash
git add pyproject.toml scripts/e2e_matrix.sh README.md
git commit -m "$(cat <<'EOF'
Declare LLM optional deps, extend e2e matrix, document Ollama recipe

pyproject.toml gains three extras: [anthropic], [openai], [all].
Users install only what they need.

scripts/e2e_matrix.sh gains a new pytest block for all sub-project 2
test files. Integration test stays gated on AFTERAGENT_LLM_LIVE_TEST
and is auto-skipped in CI.

README.md gains an LLM-driven diagnosis section with zero-config,
per-run override, auto-enhance config, and Ollama dogfood recipes.

Sub-project 2 task 13/14.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Manual dogfood acceptance check

**Files:** none (manual verification)

This is the quality-bar acceptance test for sub-project 2. If LLM-authored interventions come back as generic boilerplate rather than run-specific text, the prompts need tuning.

- [ ] **Step 14.1: Ensure an LLM provider is configured**

Either set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` in the environment, or run Ollama locally with a schema-capable model (qwen2.5-coder:7b recommended).

- [ ] **Step 14.2: Find a real captured run**

Pick an existing run in the project's `.afteragent/afteragent.sqlite3` — ideally one with non-empty rule-based findings and a non-empty diff. List runs with:

```bash
afteragent runs
```

Pick a run id. If no real run exists yet, capture a fresh one:

```bash
afteragent exec -- claude "read the README and improve one section"
```

- [ ] **Step 14.3: Run the enhancement**

```bash
afteragent enhance <run-id>
```

Expected output: one line with `Enhanced run <id>: +N findings, M intervention(s) (X in / Y out tokens, $Z)`.

If the exit code is non-zero, inspect the output for the error message. Common causes: missing API key, schema-capable model not selected for Ollama, rate limit from the provider.

- [ ] **Step 14.4: Inspect the enhanced findings**

```bash
sqlite3 .afteragent/afteragent.sqlite3 \
  "SELECT source, code, title, substr(summary, 1, 150) FROM diagnoses WHERE run_id='<run-id>' ORDER BY source, id;"
```

Verify:
- At least one row has `source='llm'`.
- LLM summaries name specific files, tests, or review comments from the run context — not generic boilerplate.

- [ ] **Step 14.5: Inspect the enhanced interventions**

```bash
sqlite3 .afteragent/afteragent.sqlite3 \
  "SELECT source, type, title, substr(content, 1, 200) FROM interventions WHERE run_id='<run-id>' ORDER BY source, id;"
```

Verify:
- At least one row has `source='llm'`.
- LLM intervention content is specific: names files, tests, error messages, or review comments from the actual run.
- If ALL LLM interventions read as generic ("read the failing test first", "understand the requirements before editing"), the prompts need tuning — open a followup issue and stop here. Do not mark sub-project 2 as complete.

- [ ] **Step 14.6: Check the generation records**

```bash
sqlite3 .afteragent/afteragent.sqlite3 \
  "SELECT kind, provider, model, input_tokens, output_tokens, estimated_cost_usd, status FROM llm_generations WHERE run_id='<run-id>' ORDER BY id;"
```

Verify:
- Two rows (one findings, one interventions).
- Both with `status='success'`.
- Token counts and cost look reasonable.

- [ ] **Step 14.7: Verify graceful degradation**

Temporarily unset the API key and re-run `afteragent exec` on a simple command:

```bash
unset ANTHROPIC_API_KEY  # or whichever key is active
afteragent exec -- python3 -c "print('test')"
```

Expected: the run captures and produces rule-based findings just like before. No errors about missing LLM. No warnings unless `auto_enhance_on_exec = true` is set in config. Then restore the key.

- [ ] **Step 14.8: Tag sub-project 2 complete**

If all previous steps passed, the sub-project is done:

```bash
git tag subproject-2-complete
```

Otherwise, document the specific acceptance failure in a followup issue and leave sub-project 2 open.

---

## Self-review checklist (plan author)

**1. Spec coverage:**
- [x] Goal 1 (4 providers): Task 5 implements both client adapters.
- [x] Goal 2 (LLMClient Protocol): Task 4.
- [x] Goal 3 (schemas with origin field): Task 6.
- [x] Goal 4 (enhance_diagnosis_with_llm): Task 10.
- [x] Goal 5 (llm_generations table): Task 1.
- [x] Goal 6 (config surface + auto-detect): Task 2.
- [x] Goal 7 (opt-in invocation): Task 12 (CLI + --enhance flags + auto_enhance_on_exec).
- [x] Goal 8 (never-break-the-run): Task 10's error handling + Task 11's fallback + test_llm_enhancer failure-path tests.
- [x] Non-goals respected: no UI changes, no effectiveness pruning, no caching, no multi-turn loops, no redaction, no aggregate cost command.

**2. Placeholder scan:**
- No "TBD" / "TODO" / "implement later" / "similar to Task N".
- Every code-change step includes the actual code.
- Every commit message is written in full.

**3. Type consistency:**
- `LLMConfig` field list matches between Task 2 (definition) and Tasks 5, 10, 12 (usage).
- `StructuredResponse` field list matches between Task 4 (definition) and Tasks 5, 10 (usage).
- `MergedFinding` field list matches between Task 8 (definition) and Tasks 9, 10 (usage).
- `EnhanceResult` field list matches between Task 10 (definition) and Task 12 (usage).
- `get_client(config)` signature matches across Tasks 4, 5, 12.
- `enhance_diagnosis_with_llm(store, run_id, client, config)` signature matches Tasks 10 and 12.
- `build_diagnosis_prompt` / `build_interventions_prompt` return `(system, user)` tuples consistently in Tasks 8 and 10.
- `merge_findings(rule_findings, llm_findings) -> list[MergedFinding]` matches Tasks 9 and 10.
- Source tag values (`"rule"`, `"llm"`) used consistently across Tasks 1, 9, 10.

**4. Known plan imperfections (acknowledged, not blocking):**
- Task 11's test file is separate from the existing `test_diagnostics.py` to avoid stepping on unittest-style tests there. The test file name `test_diagnostics_llm_hook.py` is explicit about its scope.
- Task 13 references both `[project.optional-dependencies]` and requires reading the current state of pyproject.toml to avoid duplicating the `dev` extra if Task 6 already added it. The implementer should merge, not replace.
- Task 14 requires a real LLM provider and a real captured run — if dogfooding on a fresh checkout, the engineer needs to run `afteragent exec` at least once before `afteragent enhance` can do anything meaningful.

