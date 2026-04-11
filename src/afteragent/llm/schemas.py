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
