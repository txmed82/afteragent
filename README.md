# AfterAction

AfterAction helps you understand why an agent run went wrong, what it missed, and what to change before the next attempt.

It captures runs, pulls in GitHub PR context, diagnoses repeat failure patterns, and turns that diagnosis into practical interventions such as prompt exports, repo instruction updates, and replay context.

## Why this exists

Agent-assisted PR repair often fails in familiar ways:

- the agent keeps fixing the wrong files
- failing CI checks never make it into the plan
- review comments stay unresolved across attempts
- the next run starts without the lessons from the last one

AfterAction makes those mistakes visible and reusable. Instead of treating each run like a fresh start, it records the run, analyzes the failure surface, and prepares the next attempt with better context.

## Who it's for

- engineers using coding agents to fix pull requests
- teams experimenting with Claude Code, Codex, OpenClaw, or plain CLI runners
- people who want a local audit trail for agent behavior
- anyone trying to turn trial-and-error repair loops into a more disciplined workflow

## What it does

- captures command runs, output, exit codes, diffs, events, and artifacts
- ingests GitHub PR context including changed files, review threads, checks, workflow runs, and CI log excerpts
- diagnoses patterns like repeated failures, low overlap with failing files, ignored review comments, and broad edit drift
- exports interventions as task prompts, runner context, replay context, and repo instruction patches
- applies repo instruction updates to files like `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `CURSOR.md`, and `COPILOT.md`
- replays a prior run with intervention context injected into the environment
- scores replay outcomes so you can tell whether the next attempt improved or regressed
- serves a local UI for runs, findings, interventions, and replay comparisons

## Runner support

AfterAction is runner-agnostic by default.

- `shell`: fallback mode for any CLI command
- `openclaw`: OpenClaw-specific targeting and transcript parsing
- `claude-code`: Claude Code instruction targeting and transcript parsing
- `codex`: Codex instruction targeting and transcript parsing

If a runner exposes richer output, AfterAction can extract structured events such as `tool.called`, `file.edited`, and `retry.detected`. If not, the shell path still works.

## Installation

Requires Python 3.11+.

```bash
pip install afteraction
```

For development:

```bash
git clone https://github.com/txmed82/afteragent.git
cd afteraction
pip install -e .
```

## Quick start

Capture a simple run:

```bash
afteraction exec -- python3 -c "print('hello from afteraction')"
```

List runs and inspect one:

```bash
afteraction runs
afteraction show <run-id>
afteraction diagnose <run-id>
```

Open the local viewer:

```bash
afteraction ui
```

By default, AfterAction stores everything in `.afteraction/` in the current repository.

## Common use cases

### 1. Capture a local agent run

Use this when you already know what command you want to run and you want the full trace.

```bash
afteraction exec -- codex run "Fix the failing tests"
afteraction exec -- openclaw repair
afteraction exec -- python3 scripts/repair.py
```

### 2. Snapshot a live pull request before making changes

Use this when you want the failure surface first: changed files, review threads, checks, and CI evidence.

```bash
afteraction validate-pr --repo vega/sphinxext-altair --pr 16
afteraction diagnose <run-id>
```

### 3. Export or apply interventions from a prior run

Use this when a failed run surfaced useful guidance you want to preserve.

```bash
afteraction export-interventions <run-id>
afteraction apply-interventions <run-id>
```

Typical outputs include:

- a task prompt export
- runner policy context
- replay context JSON
- repo instruction patches

### 4. Replay a run with better context

Use this when you want to retry from a known failure with interventions already loaded.

```bash
afteraction replay <run-id> -- python3 -c "import os; print(os.environ['AFTERACTION_SOURCE_RUN'])"
afteraction replay --runner claude-code <run-id> -- claude "Fix the failing PR"
afteraction replay --runner codex <run-id> -- codex run "Address review comments"
```

### 5. Run the full repair loop in one command

Use this when you want to validate a PR, apply interventions, and launch the next attempt in one step.

```bash
afteraction attempt-repair --repo vega/sphinxext-altair --pr 16 -- python3 -c "print('repair')"
afteraction attempt-repair --run-id <run-id> --runner openclaw -- openclaw repair
```

## Typical workflow

For a live pull request:

```bash
afteraction validate-pr --repo owner/name --pr 123
afteraction diagnose <validation-run-id>
afteraction apply-interventions <validation-run-id>
afteraction replay --runner claude-code <validation-run-id> -- claude "Fix the PR"
afteraction ui
```

For an existing failed local run:

```bash
afteraction diagnose <run-id>
afteraction export-interventions <run-id>
afteraction replay <run-id> -- python3 scripts/repair.py
```

## What the replay score means

AfterAction compares a replay against its source run and records whether things improved or got worse.

The score takes into account signals such as:

- run status and exit code
- number of findings
- failing check count
- failure-file count
- unresolved review-thread surface
- overlap between edits and the known failure surface

That makes it easier to answer a simple question: did the intervention actually help?

## Files you will see

Inside `.afteraction/` you will typically find:

- `afteraction.sqlite3`: run metadata and event history
- `artifacts/`: stdout, stderr, git diffs, GitHub context, and CI logs
- `exports/`: exported intervention sets
- `applied/`: applied instruction patches and manifests
- `replays/`: replay input bundles and manifests

You may also see repo instruction files updated in the project root, depending on the runner and what already exists:

- `AGENTS.md`
- `CLAUDE.md`
- `GEMINI.md`
- `CURSOR.md`
- `COPILOT.md`

## End-to-end test matrix

The repository includes an end-to-end matrix that exercises the subprocess path and runner adapters.

```bash
./scripts/e2e_matrix.sh
```

That script runs:

- the full unit and integration suite
- the fixture-backed end-to-end tests for shell, OpenClaw, Claude Code, and Codex flows

## Current shape

AfterAction is currently local-first:

- local SQLite storage
- filesystem artifacts
- GitHub context pulled through `gh`
- small built-in UI served from the CLI

That keeps the loop inspectable and easy to run in a normal repository without extra infrastructure.
