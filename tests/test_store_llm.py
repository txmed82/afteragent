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
