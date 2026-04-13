from __future__ import annotations

from dataclasses import dataclass

from .models import PatternFinding


@dataclass(frozen=True, slots=True)
class Recommendation:
    key: str
    kind: str
    title: str
    rationale: str
    install_command: list[str] | None = None
    setup_command: list[str] | None = None


def recommend_tools(
    findings: list[PatternFinding],
    task_prompt: str,
) -> list[Recommendation]:
    codes = {finding.code for finding in findings}
    prompt = task_prompt.lower()
    recommendations: list[Recommendation] = []

    if {"active_ci_failures_present", "agent_command_failure_hidden"} & codes:
        recommendations.append(
            Recommendation(
                key="github-pr-ci",
                kind="mcp",
                title="GitHub PR/CI tooling",
                rationale="This run hit CI or verification failures. Direct PR/check tooling would have shortened the diagnosis loop.",
            )
        )

    if {"agent_zero_meaningful_activity", "agent_read_edit_divergence"} & codes:
        recommendations.append(
            Recommendation(
                key="research-skill",
                kind="skill",
                title="Repository research skill",
                rationale="The run shows weak grounding in the codebase. A research/search-oriented skill would help gather context before editing.",
            )
        )

    if any(token in prompt for token in ("ui", "frontend", "react", "tailwind", "css")):
        recommendations.append(
            Recommendation(
                key="frontend-design",
                kind="skill",
                title="frontend-design",
                rationale="The task prompt looks UI-heavy. A frontend design skill would improve quality and reduce generic implementation choices.",
                install_command=[
                    "npx",
                    "skills",
                    "add",
                    "https://github.com/pbakaus/impeccable",
                    "--skill",
                    "frontend-design",
                ],
            )
        )

    if any(token in prompt for token in ("openai", "gpt", "responses api", "chatgpt", "model")):
        recommendations.append(
            Recommendation(
                key="openai-docs",
                kind="skill",
                title="openai-docs",
                rationale="This task is API/model-docs sensitive. An official docs skill would reduce stale or guessed implementation choices.",
            )
        )

    if any(token in prompt for token in ("browser", "scrape", "website", "dom", "page")):
        recommendations.append(
            Recommendation(
                key="browser-mcp",
                kind="mcp",
                title="browser-use MCP",
                rationale="The task prompt references browser/page interactions. A browser automation MCP would provide a cleaner execution surface.",
            )
        )

    unique: list[Recommendation] = []
    seen: set[str] = set()
    for item in recommendations:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
    return unique
