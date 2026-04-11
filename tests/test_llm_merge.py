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
    assert merged[0].source == "llm"
    assert "src/foo.py:42" in merged[0].summary
    assert "cited tests/test_foo.py" in merged[0].evidence


def test_rejected_rule_removes_rule_from_merged_list():
    rules = [_rule("false_positive"), _rule("keep_me")]
    llm = [_llm("false_positive", origin="rejected_rule", rule_code_ref="false_positive",
                 summary="this rule doesn't apply because X")]

    merged = merge_findings(rules, llm)
    codes = [m.code for m in merged]
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
    llm = []

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
    rules = [_rule("rule_a")]
    llm = [_llm("wrong", origin="rejected_rule", rule_code_ref="nonexistent",
                 summary="rejecting rule that isn't there")]

    merged = merge_findings(rules, llm)
    codes = sorted(m.code for m in merged)
    assert codes == ["rule_a"]
    assert merged[0].source == "rule"


def test_duplicate_novel_codes_are_both_kept():
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
