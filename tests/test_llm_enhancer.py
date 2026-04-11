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

    diagnoses = store.get_diagnoses("run1")
    assert len(diagnoses) == 1
    assert diagnoses[0]["code"] == "novel_stuck_loop"
    assert diagnoses[0]["source"] == "llm"

    interventions = store.get_interventions("run1")
    assert len(interventions) == 1
    assert interventions[0]["source"] == "llm"
    assert interventions[0]["type"] == "prompt_patch"

    gens = store.get_llm_generations("run1")
    assert len(gens) == 2
    kinds = sorted(g["kind"] for g in gens)
    assert kinds == ["findings", "interventions"]
    assert all(g["status"] == "success" for g in gens)


def test_enhance_confirmed_rule_overwrites_rule_with_llm_version(tmp_path):
    store = _make_store(tmp_path)
    _seed_minimal_run(store)
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
    # Status should be "partial" because interventions call succeeded
    assert result.status == "partial"

    diagnoses = store.get_diagnoses("run1")
    # Rule finding should be preserved
    assert any(d["code"] == "preserve_me" and d["source"] == "rule" for d in diagnoses)
    # diagnosis_error finding should be emitted
    assert any(d["code"] == "diagnosis_error" and d["source"] == "llm" and d["severity"] == "low" for d in diagnoses)

    gens = store.get_llm_generations("run1")
    # Both calls should have generation rows
    assert len(gens) == 2
    assert any(g["kind"] == "findings" and g["status"] == "error" for g in gens)
    assert any(g["kind"] == "interventions" and g["status"] == "success" for g in gens)


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
    diagnoses = store.get_diagnoses("run1")
    assert any(d["code"] == "novel" and d["source"] == "llm" for d in diagnoses)

    gens = store.get_llm_generations("run1")
    kinds_to_status = {g["kind"]: g["status"] for g in gens}
    assert kinds_to_status.get("findings") == "success"
    assert kinds_to_status.get("interventions") == "error"