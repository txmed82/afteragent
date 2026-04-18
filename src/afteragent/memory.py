from __future__ import annotations

import re

from .models import Intervention, PatternFinding
from .models import now_utc
from .store import Store


_WORD_RE = re.compile(r"[A-Za-z0-9_./-]{3,}")


def _keywords(*values: str) -> set[str]:
    words: set[str] = set()
    for value in values:
        for match in _WORD_RE.findall(value.lower()):
            words.add(match)
    return words


def create_memories_for_run(
    store: Store,
    run_id: str,
    findings: list[PatternFinding],
    interventions: list[Intervention],
    transcript_excerpt: str,
) -> list[int]:
    created_ids: list[int] = []
    created_at = now_utc()
    for finding in findings:
        title = f"finding:{finding.code}"
        if store.find_memory_by_title(title):
            continue
        created_ids.append(
            store.create_memory(
                kind="durable_rule",
                title=title,
                summary=finding.title,
                content=f"{finding.summary}\n" + "\n".join(finding.evidence[:3]),
                source_run_id=run_id,
                confidence=0.8 if finding.severity == "high" else 0.6,
                scope="repo",
                created_at=created_at,
                links=[("finding_code", finding.code)],
            )
        )
    for intervention in interventions:
        title = f"intervention:{intervention.title}"
        if store.find_memory_by_title(title):
            continue
        # Map intervention.type to memory kind
        if intervention.type == "instruction_patch":
            memory_kind = "successful_fix"
        elif intervention.type == "prompt_patch":
            memory_kind = "prompt_change"
        else:
            # For runtime_guardrail, recommended_tool, and others
            memory_kind = "recommended_tooling"
        created_ids.append(
            store.create_memory(
                kind=memory_kind,
                title=title,
                summary=intervention.title,
                content=intervention.content,
                source_run_id=run_id,
                confidence=0.55,
                scope="repo",
                created_at=created_at,
                links=[("intervention_target", intervention.target)],
            )
        )
    if transcript_excerpt.strip():
        title = f"excerpt:{run_id}"
        if store.find_memory_by_title(title) is None:
            created_ids.append(
                store.create_memory(
                    kind="transcript_excerpt",
                    title=title,
                    summary="Key transcript excerpt from finalized run",
                    content=transcript_excerpt.strip(),
                    source_run_id=run_id,
                    confidence=0.5,
                    scope="repo",
                    created_at=created_at,
                    links=[("run_id", run_id)],
                )
            )
    return created_ids


def retrieve_memories(store: Store, run_id: str, task_prompt: str, limit: int = 5) -> list[dict]:
    # Get repository_id from the run
    run = store.get_run(run_id)
    repository_id = run.cwd if run else None

    prompt_keywords = _keywords(task_prompt)
    hits: list[tuple[float, dict]] = []
    for memory in store.list_memories(limit=100, repository_id=repository_id):
        haystack = _keywords(memory.title, memory.summary, memory.content)
        overlap = prompt_keywords & haystack
        if not overlap:
            continue
        score = float(len(overlap)) + float(memory.confidence)
        hit = {
            "id": memory.id,
            "kind": memory.kind,
            "title": memory.title,
            "summary": memory.summary,
            "content": memory.content,
            "score": score,
            "reason": f"keyword overlap: {', '.join(sorted(overlap)[:5])}",
        }
        hits.append((score, hit))
    hits.sort(key=lambda item: item[0], reverse=True)
    chosen = [item[1] for item in hits[:limit]]
    for item in chosen:
        store.record_memory_hit(run_id, item["id"], item["reason"], item["score"])
    return chosen