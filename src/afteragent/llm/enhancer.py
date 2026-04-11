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

    try:
        context = load_diagnosis_context(store, run_id)
    except Exception as exc:
        errors.append(f"failed to load diagnosis context: {exc}")
        return EnhanceResult(
            status="error",
            findings_count=0,
            interventions_count=0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=0.0,
            error_messages=errors,
        )

    # ----- Findings call -----
    system, user = build_diagnosis_prompt(context)
    merged: list[MergedFinding] | None = None
    findings_succeeded = False
    llm_findings_raw: list[dict] = []
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

        llm_findings_raw = response.data.get("findings", [])
        merged = merge_findings(context.rule_findings, llm_findings_raw)
        findings_count = len(merged)
        findings_succeeded = True

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
        # Emit diagnosis_error finding and preserve rule-based findings
        # Create merged list from rule findings plus the diagnosis_error finding
        merged = [
            MergedFinding(
                code=r.code,
                title=r.title,
                severity=r.severity,
                summary=r.summary,
                evidence=list(r.evidence),
                source="rule",
            )
            for r in context.rule_findings
        ]
        # Add diagnosis_error finding
        merged.append(
            MergedFinding(
                code="diagnosis_error",
                title="LLM diagnosis call failed",
                severity="low",
                summary=f"LLM findings call failed: {exc}",
                evidence=[],
                source="llm",
            )
        )
        findings_count = len(merged)

    # ----- Interventions call -----
    system, user = build_interventions_prompt(context, merged)
    llm_interventions: list[dict] = []
    interventions_succeeded = False
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
        interventions_succeeded = True
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
        # Fall back to empty interventions (hardcoded interventions would be handled by caller)
        interventions_count = 0

    # ----- Persist -----
    # Rule findings that the LLM confirmed (overriding with its own version) or
    # rejected must be removed from the store so the LLM version (or nothing)
    # is the single authoritative row after this pass.
    # Only remove rules that were explicitly referenced by the LLM via rule_code_ref.
    rule_codes_to_remove: list[str] = []
    if findings_succeeded and llm_findings_raw:
        referenced_rule_codes = set()
        for llm_f in llm_findings_raw:
            rule_code_ref = llm_f.get("rule_code_ref")
            if rule_code_ref and llm_f.get("origin") in ("confirmed_rule", "rejected_rule"):
                referenced_rule_codes.add(rule_code_ref)

        # Remove rules that were explicitly referenced (confirmed or rejected)
        rule_codes_to_remove = list(referenced_rule_codes)

    try:
        store.replace_llm_diagnosis(
            run_id=run_id,
            findings_rows=[
                _merged_finding_to_row(run_id, f) for f in merged if f.source == "llm"
            ],
            interventions_rows=[
                _intervention_dict_to_row(run_id, i) for i in llm_interventions
            ],
            rule_codes_to_remove=rule_codes_to_remove,
        )
    except Exception as exc:
        errors.append(f"failed to persist diagnosis: {exc}")
        return EnhanceResult(
            status="error",
            findings_count=0,
            interventions_count=0,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_cost_usd=total_cost,
            error_messages=errors,
        )

    # Compute status based on success flags, not merged truthiness
    if not errors:
        status = "success"
    elif findings_succeeded or interventions_succeeded:
        status = "partial"
    else:
        status = "error"
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