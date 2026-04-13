from __future__ import annotations

from dataclasses import dataclass
import re


_CRITICAL_PATTERNS = (
    re.compile(r"\b(error|failed|failure|assertionerror|traceback|exception)\b", re.I),
    re.compile(r"\btests?[/.:][^\s]+", re.I),
    re.compile(r"[A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|go|rb|rs|java|kt|json|ya?ml|toml|md|rst)(?::\d+)?"),
)


@dataclass(slots=True)
class CompressionResult:
    artifact_kind: str
    strategy: str
    original_text: str
    compressed_text: str
    preserved_lines: list[str]
    fallback_reason: str | None = None

    @property
    def original_size(self) -> int:
        return len(self.original_text)

    @property
    def compressed_size(self) -> int:
        return len(self.compressed_text)

    @property
    def preserved_count(self) -> int:
        return len(self.preserved_lines)

    @property
    def estimated_original_tokens(self) -> int:
        return estimate_tokens(self.original_text)

    @property
    def estimated_compressed_tokens(self) -> int:
        return estimate_tokens(self.compressed_text)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _dedupe_preserve_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    return deduped


def _critical_lines(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.search(stripped) for pattern in _CRITICAL_PATTERNS):
            kept.append(stripped)
    return _dedupe_preserve_order(kept)


def compress_text(
    artifact_kind: str,
    text: str,
    *,
    max_lines: int = 40,
    max_chars: int = 4000,
) -> CompressionResult:
    if not text.strip():
        return CompressionResult(
            artifact_kind=artifact_kind,
            strategy="empty",
            original_text=text,
            compressed_text="",
            preserved_lines=[],
            fallback_reason="empty",
        )

    lines = text.splitlines()
    critical = _critical_lines(lines)
    head = [line.strip() for line in lines[:5] if line.strip()]
    tail = [line.strip() for line in lines[-10:] if line.strip()]
    compressed_lines = _dedupe_preserve_order(critical + head + tail)

    if len(compressed_lines) > max_lines:
        compressed_lines = compressed_lines[:max_lines]
    compressed = "\n".join(compressed_lines).strip()
    if len(compressed) > max_chars:
        compressed = compressed[: max_chars - 1] + "…"

    strategy = "deterministic"
    fallback_reason = None
    if compressed == text.strip():
        strategy = "passthrough"
        fallback_reason = "already_within_budget"
    return CompressionResult(
        artifact_kind=artifact_kind,
        strategy=strategy,
        original_text=text,
        compressed_text=compressed,
        preserved_lines=critical,
        fallback_reason=fallback_reason,
    )


def build_context_bundle(
    blocks: list[tuple[str, str]],
    *,
    token_budget: int = 3000,
) -> tuple[str, list[CompressionResult]]:
    results = [compress_text(kind, text) for kind, text in blocks if text.strip()]
    chosen: list[str] = []
    spent = 0
    for result in results:
        block = f"## {result.artifact_kind}\n\n{result.compressed_text}".strip()
        block_tokens = estimate_tokens(block)
        if spent and spent + block_tokens > token_budget:
            break
        spent += block_tokens
        chosen.append(block)
    return ("\n\n".join(chosen).strip(), results)
