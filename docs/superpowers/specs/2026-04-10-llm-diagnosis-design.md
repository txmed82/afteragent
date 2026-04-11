# Sub-Project 2: LLM-Driven Diagnosis and Intervention Authoring — Design

**Status:** Design approved, pending spec review
**Date:** 2026-04-10
**Owner:** Colin
**Scope:** Sub-project 2 of 5 in the AfterAgent self-improvement arc.
**Depends on:** Sub-project 1 (transcript ingestion layer)

---

## Context

Sub-project 1 added a normalized transcript event layer that captures rich per-runner agent activity. It produces structured events in a new `transcript_events` table, preserves raw transcripts as artifacts, and works across Claude Code (rich JSONL), Codex (stdout regex), and any other CLI (generic heuristic fallback). It changes no user-visible behavior — it only builds the signal foundation that sub-projects 2–5 consume.

Sub-project 2 takes that foundation and adds **LLM-driven diagnosis and intervention authoring**. The existing `diagnostics.py` has 6 hardcoded regex/heuristic pattern detectors and 8 hardcoded intervention text strings. Sub-project 2 introduces an LLM layer that:

1. **Reviews and augments** the rule-based findings — keeps them as a cheap floor, lets the LLM reject false positives for this specific run, and adds novel findings the rules can't detect.
2. **Replaces** the hardcoded intervention strings with LLM-authored text that names specific files, tests, and review comments from the actual run context.

Both are gated on having an LLM provider configured. When none is configured, AfterAgent falls back to the existing rule-based and hardcoded-string behavior. Zero-config users get exactly what they have today; users who opt in get a meaningful quality jump.

Sub-project 2 does NOT run the LLM automatically on every `analyze_run` call — every UI page load, every CLI subcommand, every replay scoring invocation would otherwise trigger an LLM call. Instead, sub-project 2 adds a new `enhance_diagnosis_with_llm` function that writes back to the store with source-tagged findings, and gates its invocation behind explicit commands or a config flag.

Sub-projects 3–5 consume this layer:
- **Sub-project 3** effectiveness-driven pruning uses the `source` column (rule vs llm) and the new `llm_generations` table to learn which finding sources and intervention types actually lead to improved replays.
- **Sub-project 4** broadening past PR repair reuses the same enhancement path for non-PR agent runs.
- **Sub-project 5** narrative UI surfaces the source-tagged findings and cost-per-run attribution in the browser.

## Goals

1. Support four LLM providers in v1: Anthropic, OpenAI, OpenRouter, Ollama. Implementation covers two API shapes (Anthropic native + OpenAI-compatible), so three of the four providers share one client adapter.
2. Provide a thin `LLMClient` Protocol that returns provider-agnostic structured responses. Callers in `enhancer.py` never touch provider-specific SDK types.
3. Ship structured-output schemas (`FINDINGS_SCHEMA`, `INTERVENTIONS_SCHEMA`) that include an `origin` field on findings so the orchestrator can merge LLM output against rule-based findings via confirm/reject/novel classification.
4. Add `enhance_diagnosis_with_llm(store, run_id, client)` that loads run context, composes a budgeted prompt (~8k–15k tokens), calls the LLM for findings, merges against rule-based findings, calls the LLM for interventions, and persists everything to the store with source tags.
5. Add a new `llm_generations` table recording provider, model, token counts, duration, and estimated cost per LLM call — the foundation for sub-project 3's effectiveness pruning.
6. Add a config surface (`.afteragent/config.toml` + env vars + CLI flags) with auto-detect defaults so users with an `ANTHROPIC_API_KEY` already set get working LLM diagnosis with zero additional configuration.
7. Make LLM invocation explicitly opt-in. Default behavior of `afteragent exec` is unchanged (rule-based only). Auto-enhancement is a config flag, not the default.
8. Preserve the never-break-the-run contract from sub-project 1: every LLM failure path is non-destructive. Rule-based findings are never clobbered by a failed LLM enhancement.

## Non-goals

- **No UI changes.** Sub-project 5 owns surfacing LLM findings in the browser with source tags and cost attribution.
- **No effectiveness-driven pruning.** The `source` column and `llm_generations` table are prerequisites for sub-project 3, but sub-project 2 itself doesn't feed win-rate back into either layer.
- **No prompt caching.** Anthropic's beta prompt caching and OpenAI's prefix caching are cost optimizations deferred until we have real usage data.
- **No multi-turn agent loops.** Two one-shot LLM calls per run (findings, interventions). Not an agent orchestration layer.
- **No follow-up context expansion.** The LLM gets a pre-budgeted context window; it cannot request additional files or tool_result bodies on demand. That's a future sub-project if diagnosis quality demands it.
- **No fine-tuning, no embeddings, no RAG over historical runs.** Plain structured prompting. Historical runs appear only via the "related runs on this PR" summary.
- **No `afteragent config set` command.** Users edit `.afteragent/config.toml` themselves or use env vars.
- **No aggregate cost reporting command.** `llm_generations` rows are queryable; aggregation is a sub-project 5 UI concern.
- **No retry logic beyond what the underlying SDKs provide.** Both `anthropic` and `openai` SDKs retry transient failures. We don't add our own retry layer in v1.
- **No backfill of LLM enhancement on historical runs.** Only new runs or explicit `afteragent enhance <run-id>` invocations get LLM findings.
- **No interactive streaming.** User waits for the LLM call to complete. No token-by-token output.
- **No secret redaction of stdout/stderr before prompting.** The user is responsible for what they send to their configured provider. Documented as a known limitation.
- **No background/async enhancement mode in v1.** `auto_enhance_on_exec` runs synchronously at the end of `afteragent exec`. Async mode is a follow-up.

## Architecture

### New package: `src/afteragent/llm/`

A Python package, not a single file — sub-project 2 introduces enough distinct concerns that bundling them into one module would produce an unwieldy ~700-line file.

```
src/afteragent/llm/
├── __init__.py           # Public exports
├── client.py             # LLMClient Protocol + get_client() factory
├── anthropic_client.py   # AnthropicClient using `anthropic` SDK
├── openai_client.py      # OpenAICompatClient using `openai` SDK
├── config.py             # LLMConfig dataclass + load_config()
├── schemas.py            # FINDINGS_SCHEMA, INTERVENTIONS_SCHEMA
├── prompts.py            # build_diagnosis_prompt, build_interventions_prompt
├── enhancer.py           # enhance_diagnosis_with_llm() orchestration
└── cost_table.py         # Per-model pricing for estimated_cost_usd
```

Each file has one clear responsibility. The client adapters use lazy imports so users with only `afteragent[anthropic]` installed don't fail at import time if they never instantiate the OpenAI-compat client.

### Modified existing files

| File | Change |
|---|---|
| `src/afteragent/diagnostics.py` | `analyze_run` stays exactly as-is (rule-based path, unchanged). `build_interventions` is refactored to take an optional `llm_interventions` parameter — if provided, they're used; otherwise the hardcoded fallback runs. A new module-level function `persist_llm_enhanced_diagnosis(store, run_id, findings, interventions)` writes findings with `source="llm"` tags. |
| `src/afteragent/store.py` | Additive migration: `diagnoses` and `interventions` tables each gain a `source TEXT NOT NULL DEFAULT 'rule'` column. New table `llm_generations` with appropriate columns and indexes. New methods: `replace_llm_diagnosis`, `record_llm_generation`, `get_llm_generations`. Existing `replace_diagnosis` is updated to tag inserts with `source="rule"`. |
| `src/afteragent/cli.py` | New `enhance` subcommand: `afteragent enhance <run-id>`. Existing `exec` subcommand gains `--enhance` and `--no-enhance` flags for per-call override of the config default. |
| `src/afteragent/config.py` | `AppPaths` gains a `config_path` field pointing at `.afteragent/config.toml`. |
| `pyproject.toml` | Adds `[project.optional-dependencies]` with `anthropic`, `openai`, and `all` extras. `tomllib` from stdlib (Python ≥3.11) handles config parsing, no new runtime dep. |

### Unchanged

`capture.py`, `adapters.py`, `workflow.py` (mostly — one small change to pass enhanced findings through `export_interventions` when they exist), `ui.py`, `models.py`, `transcripts.py`, `github.py`. No UI changes; no transcript-layer changes.

## Configuration

### `.afteragent/config.toml`

```toml
[llm]
provider = "anthropic"              # "anthropic" | "openai" | "openrouter" | "ollama"
model = "claude-sonnet-4-5"

# Auto-invoke enhance_diagnosis_with_llm at the end of every afteragent exec.
# Default: false. Users opt in explicitly.
auto_enhance_on_exec = false

# Optional:
# base_url    — only needed for openai/openrouter/ollama if overriding the SDK default
# max_tokens  — structured response size cap, default 4096
# temperature — default 0.2
# timeout_s   — default 60
```

Users without this file get rule-based behavior everywhere, silently.

### Environment variables

| Variable | Use |
|---|---|
| `AFTERAGENT_LLM_PROVIDER` | Overrides `provider` field |
| `AFTERAGENT_LLM_MODEL` | Overrides `model` field |
| `AFTERAGENT_LLM_BASE_URL` | Overrides `base_url` field |
| `ANTHROPIC_API_KEY` | Required when provider=anthropic |
| `OPENAI_API_KEY` | Required when provider=openai |
| `OPENROUTER_API_KEY` | Required when provider=openrouter |
| `OLLAMA_BASE_URL` | Default `http://localhost:11434/v1`, required when provider=ollama and non-default |

**API keys come only from env vars, never from the config file.** The config file is meant to be checked into a repo; keys must not be.

### Precedence

1. CLI flags (`--llm-provider`, `--llm-model`, `--llm-base-url`)
2. Env vars (`AFTERAGENT_LLM_*`)
3. Config file (`.afteragent/config.toml`)
4. Auto-detect: if `ANTHROPIC_API_KEY` → anthropic + claude-sonnet-4-5; else if `OPENAI_API_KEY` → openai + gpt-4o-mini; else if `OPENROUTER_API_KEY` → openrouter + anthropic/claude-3.5-sonnet; else if `OLLAMA_BASE_URL` reachable → ollama + llama3.1:8b; else → no LLM config, rule-based only.

### CLI surface

```
afteragent enhance <run-id>                  # Manual LLM enhancement of an existing run
afteragent exec --enhance -- claude "..."    # Force LLM enhancement for this exec, even if config disabled
afteragent exec --no-enhance -- claude "..." # Skip LLM enhancement for this exec, even if config enabled
```

`afteragent diagnose` stays rule-based only.

## LLMClient abstraction

```python
# src/afteragent/llm/client.py

@dataclass(slots=True)
class StructuredResponse:
    data: dict
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    duration_ms: int
    raw_response_excerpt: str  # First 500 chars for debugging


class LLMClient(Protocol):
    name: str   # "anthropic" | "openai-compat"
    model: str

    def call_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
    ) -> StructuredResponse: ...


def get_client(config: LLMConfig) -> LLMClient:
    if config.provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(config)
    if config.provider in ("openai", "openrouter", "ollama"):
        from .openai_client import OpenAICompatClient
        return OpenAICompatClient(config)
    raise ValueError(f"Unknown provider: {config.provider}")
```

`AnthropicClient` uses the Messages API with `tool_choice={"type": "tool", "name": tool_name}` to force a structured `tool_use` block. Returns `tool_use.input` as the `data` field.

`OpenAICompatClient` uses `chat.completions.create` with `response_format={"type": "json_schema", "json_schema": {"name": tool_name, "schema": schema, "strict": True}}`. Works unchanged for OpenAI, OpenRouter, and Ollama by varying `base_url`. Strict JSON schema mode is supported natively by OpenAI, passthrough by OpenRouter, and by recent Ollama models (qwen2.5-coder, llama 3.1+). Documented as a compatibility note.

Ollama's no-API-key edge case is handled by passing an empty placeholder to the `openai` SDK.

## Structured output schemas

### `FINDINGS_SCHEMA`

```json
{
  "type": "object",
  "properties": {
    "findings": {
      "type": "array",
      "maxItems": 12,
      "items": {
        "type": "object",
        "properties": {
          "code": {"type": "string"},
          "title": {"type": "string", "maxLength": 120},
          "severity": {"enum": ["low", "medium", "high"]},
          "summary": {"type": "string", "maxLength": 500},
          "evidence": {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
            "maxItems": 8
          },
          "origin": {"enum": ["confirmed_rule", "rejected_rule", "novel"]},
          "rule_code_ref": {"type": ["string", "null"]}
        },
        "required": ["code", "title", "severity", "summary", "evidence", "origin", "rule_code_ref"],
        "additionalProperties": false
      }
    }
  },
  "required": ["findings"],
  "additionalProperties": false
}
```

The `origin` enum is the review mechanic: `confirmed_rule` keeps a rule-based finding (with the LLM's personalized summary/evidence), `rejected_rule` marks it as a false positive for this specific run, `novel` introduces a new finding. The merge logic in `enhancer.py` uses `rule_code_ref` to find the stored rule finding when confirming or rejecting.

### `INTERVENTIONS_SCHEMA`

```json
{
  "type": "object",
  "properties": {
    "interventions": {
      "type": "array",
      "maxItems": 10,
      "items": {
        "type": "object",
        "properties": {
          "type": {
            "enum": [
              "instruction_patch",
              "prompt_patch",
              "context_injection_rule",
              "runtime_guardrail",
              "tool_policy_rule"
            ]
          },
          "title": {"type": "string", "maxLength": 120},
          "target": {"enum": ["repo_instructions", "task_prompt", "runner_context", "runner_policy"]},
          "content": {"type": "string", "maxLength": 2000},
          "scope": {"enum": ["pr", "repo", "run"]},
          "related_finding_codes": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["type", "title", "target", "content", "scope", "related_finding_codes"],
        "additionalProperties": false
      }
    }
  },
  "required": ["interventions"],
  "additionalProperties": false
}
```

The `type` and `target` enums reuse the existing intervention vocabulary in `workflow.py` so LLM-authored interventions plug into `export_interventions` / `apply_interventions` with no other changes.

### Two calls, not one

Sub-project 2 makes **two** separate LLM calls per enhance invocation:

1. **Findings call** — system prompt focuses on diagnosis ("what went wrong?"), schema is `FINDINGS_SCHEMA`, tool name is `report_findings`.
2. **Interventions call** — system prompt focuses on remediation ("given these findings, author fixes"), schema is `INTERVENTIONS_SCHEMA`, tool name is `author_interventions`. Input context includes the merged findings from call 1.

Separate calls give each prompt a tighter system message, independent schema validation, and failure isolation (interventions call can fail while findings succeed). Cost difference is negligible — the input context dominates; output is small.

## `enhance_diagnosis_with_llm` flow

```
User runs: afteragent enhance <run-id>
  │       (or afteragent exec --enhance, or exec with auto_enhance_on_exec=true)
  ▼
1. LLMConfig.load() — walk precedence chain. If None → log + exit 0 (no-op).
2. get_client(config) — if import fails (optional dep missing), exit 1 with
   clear message.
3. Build DiagnosisContext:
     - run record (id, command, status, exit_code, duration_ms, cwd, summary)
     - rule-based findings (run analyze_run if not stored yet)
     - transcript events (from transcript_events table, all events)
     - stdout_head + stdout_tail (first/last 50 lines, capped at 5000 chars each)
     - stderr_head + stderr_tail (first/last 30 lines, capped at 3000 chars each)
     - diff_text (full diff, truncated at 20000 chars with "[diff truncated]")
     - changed_files (extracted from diff)
     - github_summary (repo, pr_number, failing_checks, unresolved_review_threads)
     - related_runs (up to 3 prior runs on the same PR, one-line summaries)
4. build_diagnosis_prompt(context) → (system, user) strings.
5. client.call_structured(system, user, FINDINGS_SCHEMA, "report_findings")
     Returns StructuredResponse with parsed findings.
6. Merge:
     - confirmed_rule → keep stored rule, overwrite summary/evidence with LLM version
     - rejected_rule  → remove the rule from the merged list
     - novel          → add as new finding with source="llm"
     Rules the LLM didn't address remain with source="rule".
7. build_interventions_prompt(context, merged_findings) → (system, user) strings.
8. client.call_structured(system, user, INTERVENTIONS_SCHEMA, "author_interventions")
     Returns StructuredResponse with parsed interventions.
9. store.replace_llm_diagnosis(run_id, merged_findings, llm_interventions)
     Writes diagnoses with source tags, writes interventions with source="llm".
10. store.record_llm_generation(...) × 2 — one row per call with provider,
    model, input_tokens, output_tokens, duration_ms, estimated_cost_usd, status,
    raw_response_excerpt.
11. Print summary line: "Enhanced run <id>: +N findings, M interventions
    authored (Xk in/Yk out tokens, $Z)"
```

### Prompt composition

Input budget targets ~8k–15k tokens for typical runs:

| Section | Typical size | Truncation |
|---|---|---|
| Rule findings (JSON array) | ~500 tokens | None (capped at 6 findings by the detector) |
| Transcript events | ~2000–5000 tokens | inputs_summary cut to 150 chars, output_excerpt to 200 chars |
| stdout head + tail | ~2000–3000 tokens | First + last 50 lines, 5000 chars each side |
| stderr head + tail | ~1000–1500 tokens | First + last 30 lines, 3000 chars each side |
| Git diff | ~2000–5000 tokens | Full, truncated at 20000 chars with marker |
| Changed files list | ~100 tokens | Full |
| GitHub PR summary | ~500–1000 tokens | Full (already small) |
| Related runs | ~200–500 tokens | 3 max, one-line each |
| System prompt | ~500 tokens | Fixed |

Hard ceiling of ~25k input tokens enforced before the call fires. If exceeded, the prompt builder trims transcript events first, then clips the diff more aggressively, then trims stdout/stderr tails.

### System prompts (summary — full text lives in `prompts.py`)

**Findings system prompt:**
- Role: failure-pattern diagnostician for AI coding agent runs.
- Task: for each rule finding, mark confirmed/rejected/novel. Also identify novel patterns the rules missed.
- Be specific: cite file paths, test names, error messages, tool call sequences.
- Use severity=high only for findings that would cause the next run to repeat the same failure.
- Max 12 findings. Quality over quantity.
- Output via `report_findings` tool.

**Interventions system prompt:**
- Role: author corrective instructions for AI coding agents.
- Task: for each confirmed finding, author interventions in the existing vocabulary (instruction_patch / prompt_patch / context_injection_rule / runtime_guardrail / tool_policy_rule).
- Rules: name specific files, tests, and review comments. Write in second person, imperative voice. Interventions must be preventative.
- Max 10 interventions.
- Output via `author_interventions` tool.

## Database schema additions

### `diagnoses` and `interventions` tables — new `source` column

```sql
ALTER TABLE diagnoses ADD COLUMN source TEXT NOT NULL DEFAULT 'rule';
ALTER TABLE interventions ADD COLUMN source TEXT NOT NULL DEFAULT 'rule';
```

Additive migration. Existing rows default to `"rule"` (which is what they are). New LLM-written rows are tagged `"llm"`.

### New `llm_generations` table

```sql
CREATE TABLE IF NOT EXISTS llm_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,               -- "findings" | "interventions"
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,             -- "success" | "error"
    error_message TEXT,
    created_at TEXT NOT NULL,
    raw_response_excerpt TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_generations_run ON llm_generations (run_id);
```

Cost estimation uses a small static table in `llm/cost_table.py` mapping `(provider, model)` → `(input_cost_per_1k, output_cost_per_1k)`. Unknown models and Ollama (local) cost $0.

## Error handling

Non-negotiable contract: **no LLM failure mode is allowed to destroy rule-based findings.** Every path is additive or non-destructive.

| Failure | Response |
|---|---|
| No LLM config + `afteragent enhance` invoked | Exit 1 with "No LLM provider configured. See `afteragent enhance --help`." |
| No LLM config + `afteragent exec` without `--enhance` | Silent. Rule-based only. |
| `auto_enhance_on_exec=true` but no config | One-line warning at end of exec. |
| Optional dep not installed (`import openai` fails) | Exit 1 with "Provider '<name>' requires `pip install afteragent[<extra>]`." |
| API key missing for configured provider | Exit 1 with "Provider '<name>' requires env var `<NAME>_API_KEY`." |
| Network / timeout / rate limit on findings call | Log, emit `diagnosis_error` finding (source="llm", severity="low"), fall back to hardcoded interventions, record failed `llm_generations` row. Rule-based findings preserved. |
| Findings response fails schema validation | Same as network error. |
| Interventions call fails after successful findings call | Persist merged findings, fall back to hardcoded interventions, record failed `llm_generations` row for the interventions call. |
| Provider returns empty findings/interventions arrays | Accept as valid. Empty is a legitimate "no issues found" answer. |
| Strict JSON schema mode rejected by provider (older Ollama models) | Surface clearly, fall back to rule-based, recommend upgrading local model. |

`diagnosis_error` is itself a finding kind (not a new schema type — it reuses the existing PatternFinding shape with `code="diagnosis_error"`, `severity="low"`). Surfaces in the UI, countable in metrics, never crashes the run.

## Testing strategy

### Unit tests (no network, mock LLM)

- `tests/test_llm_config.py` — precedence chain tested at each rung and combined. Auto-detect branches. Missing-key paths. Ollama no-key handling.
- `tests/test_llm_client.py` — `get_client()` factory dispatch. Both `AnthropicClient` and `OpenAICompatClient` tested with their underlying SDKs mocked at the module level (so tests don't require the SDKs to be installed). Asserts the mocked `messages.create` / `chat.completions.create` was called with the right schema argument.
- `tests/test_llm_schemas.py` — validates both JSON schemas are well-formed (using `jsonschema` as a dev dependency). Asserts handwritten fixture responses validate against them.
- `tests/test_llm_prompts.py` — `build_diagnosis_prompt` and `build_interventions_prompt` tested against a fixture `DiagnosisContext`. Asserts token budget stays under 25k. Asserts truncation markers appear when inputs exceed limits. Asserts rule findings section appears when present.
- `tests/test_llm_enhancer.py` — the core orchestration test. Pass a stub `LLMClient` that returns canned `StructuredResponse` objects. Covers:
  - `origin="confirmed_rule"` overwrites the stored rule finding's summary/evidence.
  - `origin="rejected_rule"` removes the rule from the merged list.
  - `origin="novel"` adds a new finding with `source="llm"`.
  - Rule findings the LLM didn't mention stay with `source="rule"`.
  - Interventions persisted with `source="llm"`.
  - `llm_generations` rows written for both calls with correct token counts.
  - Findings call failure → `diagnosis_error` finding + hardcoded fallback interventions + failed generation row.
  - Interventions call failure → merged findings still persisted, hardcoded interventions used.
  - Rule-based findings preserved across all failure modes.
- `tests/test_store_llm.py` — new store methods (`replace_llm_diagnosis`, `record_llm_generation`, `get_llm_generations`). `source` column round-trip. Migration adds the column without destroying existing rows.

### Integration test (gated)

- `tests/test_llm_live.py` — skipped unless `AFTERAGENT_LLM_LIVE_TEST=1` is set and a real provider is configured. Runs one real round-trip against a canned fixture run's context. Asserts response validates against the schema. Never runs in CI. Documented as a local/manual sanity check.

### E2E matrix

`scripts/e2e_matrix.sh` gains a new pytest block for `tests/test_llm_*.py`. The integration test file is auto-skipped without the env var, so the matrix stays deterministic in CI.

### Ollama dogfood recipe

Documented in README for local iteration:
```bash
ollama pull qwen2.5-coder:7b
mkdir -p .afteragent && cat > .afteragent/config.toml <<EOF
[llm]
provider = "ollama"
model = "qwen2.5-coder:7b"
EOF
afteragent exec -- claude "fix the failing test"
afteragent enhance <run-id>
```

Free, local, offline. Recommended path for users learning the feature without spending on API tokens.

## Success criteria

Sub-project 2 ships when **all** of the following are true:

1. `afteragent enhance <run-id>` against a captured Claude Code run via Anthropic provider produces at least one LLM-authored finding (`source="llm"`) and at least one LLM-authored intervention, with correct `origin` classification for any rule-based findings that existed.
2. Same run enhanced via OpenAI (with `OPENAI_API_KEY` + `AFTERAGENT_LLM_PROVIDER=openai`) produces schema-valid findings/interventions. Same via OpenRouter (with `OPENROUTER_API_KEY` + `--llm-base-url https://openrouter.ai/api/v1`). Same via Ollama (with `OLLAMA_BASE_URL` + a schema-capable local model like `qwen2.5-coder:7b`).
3. `llm_generations` rows are written for every LLM call with correct token counts and cost estimates.
4. When no LLM provider is configured, `afteragent exec` continues to work with rule-based findings/interventions — zero warnings, zero behavior changes from current master.
5. When LLM is configured but `auto_enhance_on_exec=false`, `afteragent exec` still runs rule-based only. LLM runs only on explicit `afteragent enhance` or `--enhance` flag.
6. When the LLM call fails (simulated via a bogus key and verified with a real bogus-key integration test), rule-based findings are preserved and the run is not broken.
7. `origin="confirmed_rule"` overwrites the rule finding's summary/evidence. `origin="rejected_rule"` removes it from the merged list. `origin="novel"` adds a new finding. Rules the LLM didn't mention remain untouched.
8. **Manual inspection acceptance:** take a real existing AfterAgent run from `.afteragent/`, run `afteragent enhance` on it, manually inspect the LLM-authored interventions, and confirm they name specific files/tests/review comments from the run context rather than generic boilerplate. This is the quality-bar acceptance test that mirrors sub-project 1's dogfood check.
9. All existing sub-project 1 tests still pass (91/91).
10. New tests pass: ~15–20 new pytest tests across config, client, schemas, prompts, enhancer, store, and the gated live test (skipped in CI).

Criterion #8 is the single most important check. If LLM interventions come back as generic "the agent should read the failing test first" rather than specific "before editing, read `tests/test_foo.py::test_add` and reconcile with the unresolved review comment at `src/arithmetic.py:42` about float vs int handling," the prompts need tuning before shipping.

## Open questions (documented, not blocking)

1. **Should `auto_enhance_on_exec=true` run sync or async?** Sync in v1 (matches current behavior, simpler). Async (background task, findings updated "lazily") is a follow-up.
2. **Should the LLM see `diagnosis_error` findings from its own previous failed attempts?** No in v1 — we strip them before composing the next prompt.
3. **How aggressively should secrets be redacted from stdout/stderr before sending to the LLM?** None in v1. Future hardening could add regex-based redaction for common patterns (`sk-...`, `ghp_...`, AWS keys).
