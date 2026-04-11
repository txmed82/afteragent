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
                "severity": "catastrophic",
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
                "origin": "made_up",
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
            for i in range(20)
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
                "type": "made_up_type",
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
                "target": "made_up_target",
                "content": "x",
                "scope": "pr",
                "related_finding_codes": [],
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, INTERVENTIONS_SCHEMA)


def test_exported_enum_constants_match_schema_enums():
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
