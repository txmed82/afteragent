# AfterAction

AfterAction is an MVP for capturing agent runs, diagnosing repeat failure
patterns, and converting them into reusable workflow improvements.

## Current scope

- `afteraction exec -- <command...>` captures a run
- `afteraction validate-pr --repo owner/name --pr 123` validates live GitHub PR ingestion
- `afteraction export-interventions <run-id>` writes prompt/policy/patch outputs
- `afteraction apply-interventions <run-id>` writes intervention artifacts and updates repo instruction files such as `AGENTS.md` or `CLAUDE.md`
- `afteraction replay <run-id> -- <command...>` forks a prior run with exported context injected via env vars
- `afteraction attempt-repair --repo owner/name --pr 123 -- <command...>` chains validation, intervention apply, and replay in one command
- stores events and artifacts in local SQLite + filesystem storage
- generates rule-based diagnoses for common PR-fix workflow failures
- outputs intervention suggestions such as instruction patches and guardrails
- resolves repo instruction patches against known agent files in the workspace (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `CURSOR.md`, `COPILOT.md`)
- tracks intervention-set versions, replay outcomes, and replay effectiveness scoring
- uses runner adapters: `shell` fallback for any CLI, plus higher-fidelity targeting for `claude`/`claude-code` and `codex`
- supports `--runner` presets on `replay` and `attempt-repair` for `shell`, `openclaw`, `claude-code`, and `codex`
- extracts runner-specific transcript events such as `tool.called`, `file.edited`, and `retry.detected` when adapter patterns match
- provides a local timeline viewer via CLI and a tiny web UI

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
afteraction exec -- python3 -c "print('hello')"
afteraction validate-pr --repo vega/sphinxext-altair --pr 16
afteraction export-interventions 22ffa5b426b1
afteraction apply-interventions 22ffa5b426b1
afteraction replay 22ffa5b426b1 -- python3 -c "import os; print(os.environ['AFTERACTION_SOURCE_RUN'])"
afteraction replay --runner claude-code 22ffa5b426b1 -- python3 -c "print('repair')"
afteraction attempt-repair --repo vega/sphinxext-altair --pr 16 -- python3 -c "import os; print(os.environ['AFTERACTION_SOURCE_RUN'])"
afteraction attempt-repair --run-id 22ffa5b426b1 --runner openclaw -- python3 -c "print('repair')"
afteraction runs
afteraction show <run-id>
afteraction diagnose <run-id>
afteraction ui
```

The default storage location is `.afteraction/` in the current repository.

## End-to-end loop

```bash
afteraction validate-pr --repo vega/sphinxext-altair --pr 16
afteraction apply-interventions <validation-run-id>
afteraction replay <validation-run-id> -- python3 -c "import os; print(os.environ['AFTERACTION_INTERVENTION_MANIFEST_PATH'])"
afteraction ui
```

The replay comparison surface records a score, verdict, resolved findings, new findings,
failing-check deltas, and failure-file deltas so you can tell whether an intervention set
actually improved the next run.
