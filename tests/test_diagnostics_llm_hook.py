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
    findings = [_finding("low_diff_overlap_with_failing_files")]
    result = build_interventions(findings)
    assert len(result) > 0
    assert any("failure surface" in i.content.lower() or "failing" in i.content.lower() for i in result)


def test_build_interventions_with_llm_uses_llm_list():
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
    findings = [_finding("low_diff_overlap_with_failing_files")]
    result = build_interventions(findings, llm_interventions=[])
    assert len(result) > 0


def test_build_interventions_with_none_llm_uses_hardcoded():
    findings = [_finding("low_diff_overlap_with_failing_files")]
    result_none = build_interventions(findings, llm_interventions=None)
    result_default = build_interventions(findings)
    assert len(result_none) == len(result_default)
