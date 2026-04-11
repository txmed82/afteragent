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
