from __future__ import annotations

import json
import shlex
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .diagnostics import analyze_run, load_related_contexts, load_run_context
from .store import Store
from .workflow import apply_interventions, attempt_repair, export_interventions, replay_run


def serve(store: Store, host: str = "127.0.0.1", port: int = 8765) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/summary":
                return self._json({"effectiveness": summarize_effectiveness(store)})

            if self.path == "/api/runs":
                payload = [
                    {
                        "id": run.id,
                        "command": run.command,
                        "status": run.status,
                        "exit_code": run.exit_code,
                        "created_at": run.created_at,
                        "duration_ms": run.duration_ms,
                        "summary": run.summary,
                    }
                    for run in store.list_runs()
                ]
                return self._json(payload)

            if self.path.startswith("/api/runs/"):
                run_id = self.path.removeprefix("/api/runs/")
                run = store.get_run(run_id)
                if not run:
                    self.send_error(404, "Run not found")
                    return
                findings, interventions = analyze_run(store, run_id)
                events = store.get_events(run_id)
                context = load_run_context(store, run_id)
                related = load_related_contexts(
                    store,
                    run.id,
                    run.cwd,
                    run.created_at,
                    context["gh_context"],
                )
                payload = {
                    "run": asdict(run),
                    "events": [
                        {
                            "event_type": event.event_type,
                            "timestamp": event.timestamp,
                            "payload": json.loads(event.payload_json),
                        }
                        for event in events
                    ],
                    "findings": [asdict(finding) for finding in findings],
                    "interventions": [asdict(intervention) for intervention in interventions],
                    "github": summarize_github_context(context["gh_context"]),
                    "related_runs": [summarize_related_run(item) for item in related],
                    "exports": summarize_output_paths(store, run_id),
                    "intervention_sets": summarize_intervention_sets(store, run_id),
                    "replays": summarize_replays(store, run_id),
                    "source_replay": summarize_source_replay(store, run_id),
                }
                return self._json(payload)

            self._html(app_html())

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/runs/"):
                self.send_error(404, "Unknown endpoint")
                return
            parts = self.path.split("/")
            if len(parts) < 5:
                self.send_error(404, "Unknown endpoint")
                return
            _, api_label, runs_label, run_id, action = parts[:5]
            if api_label != "api" or runs_label != "runs":
                self.send_error(404, "Unknown endpoint")
                return
            run = store.get_run(run_id)
            if not run:
                self.send_error(404, "Run not found")
                return
            body = self._read_json_body()
            if action == "export":
                manifest = export_interventions(store, run_id, base_dir=Path.cwd())
                return self._json({"ok": True, "run_id": run_id, "manifest": manifest})
            if action == "apply":
                manifest = apply_interventions(store, run_id, Path.cwd())
                return self._json({"ok": True, "run_id": run_id, "manifest": manifest})
            if action == "replay":
                command_text = body.get("command", "").strip()
                command = shlex.split(command_text) if command_text else None
                result = replay_run(
                    store,
                    source_run_id=run_id,
                    cwd=Path.cwd(),
                    command=command,
                    apply_interventions_first=bool(body.get("apply_interventions")),
                    stream_output=False,
                )
                return self._json({"ok": True, "run_id": str(result["run_id"])})
            if action == "attempt-repair":
                command_text = body.get("command", "").strip()
                command = shlex.split(command_text) if command_text else None
                if not command:
                    self.send_error(400, "attempt-repair requires a command")
                    return
                result = attempt_repair(
                    store,
                    cwd=Path.cwd(),
                    command=command,
                    source_run_id=run_id,
                    stream_output=False,
                )
                return self._json({"ok": True, "run_id": str(result["replay_run_id"]), "result": result})
            self.send_error(404, "Unknown action")
            return

        def _json(self, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, html: str) -> None:
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                return {}
            raw = self.rfile.read(content_length).decode()
            if not raw:
                return {}
            return json.loads(raw)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AfterAction UI: http://{host}:{port}")
    server.serve_forever()


def app_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AfterAction UI</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f1e8;
      --panel: #fffaf2;
      --line: #d8cfbf;
      --text: #28221a;
      --muted: #6d6558;
      --accent: #9a4d20;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, sans-serif;
      color: var(--text);
      background: linear-gradient(180deg, #faf6ef, #f0e7d9);
    }
    .shell {
      width: min(1180px, calc(100% - 24px));
      margin: 0 auto;
      padding: 24px 0 40px;
    }
    .hero {
      margin-bottom: 18px;
      padding: 20px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
    }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 36px; margin-bottom: 8px; }
    .layout {
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 18px;
    }
    .panel {
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
    }
    .runs { display: grid; gap: 10px; }
    .run {
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      cursor: pointer;
      background: #fff;
    }
    .run.active { border-color: var(--accent); }
    .meta, .small { color: var(--muted); font-size: 13px; }
    .timeline, .findings, .interventions, .related, .github, .exports, .sets, .replays, .effectiveness { display: grid; gap: 10px; }
    .item {
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
    }
    .compare {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .grid { display: grid; gap: 18px; }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font-size: 13px;
    }
    .actions {
      display: grid;
      gap: 10px;
    }
    .actions-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    button, input {
      font: inherit;
    }
    button {
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      cursor: pointer;
    }
    input {
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
    }
    @media (max-width: 860px) {
      .layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <h1>AfterAction</h1>
      <p>Capture agent runs, inspect the event timeline, and turn failures into explicit workflow changes.</p>
    </section>
    <div class="layout">
      <aside class="panel">
        <h2>Runs</h2>
        <div id="runs" class="runs"></div>
      </aside>
      <main class="grid">
        <section class="panel">
          <h2>Summary</h2>
          <div id="summary" class="small">Select a run.</div>
        </section>
        <section class="panel">
          <h2>Actions</h2>
          <div class="actions">
            <input id="replay-command" placeholder="Optional replay command override" />
            <div class="actions-row">
              <button id="export-btn" type="button">Export</button>
              <button id="apply-btn" type="button">Apply</button>
              <button id="replay-btn" type="button">Replay</button>
              <button id="attempt-repair-btn" type="button">Attempt Repair</button>
            </div>
            <div id="action-status" class="small">No action yet.</div>
          </div>
        </section>
        <section class="panel">
          <h2>Effectiveness</h2>
          <div id="effectiveness" class="effectiveness"></div>
        </section>
        <section class="panel">
          <h2>Timeline</h2>
          <div id="timeline" class="timeline"></div>
        </section>
        <section class="panel">
          <h2>Findings</h2>
          <div id="findings" class="findings"></div>
        </section>
        <section class="panel">
          <h2>Interventions</h2>
          <div id="interventions" class="interventions"></div>
        </section>
        <section class="panel">
          <h2>GitHub Context</h2>
          <div id="github" class="github"></div>
        </section>
        <section class="panel">
          <h2>Related Runs</h2>
          <div id="related" class="related"></div>
        </section>
        <section class="panel">
          <h2>Output Files</h2>
          <div id="exports" class="exports"></div>
        </section>
        <section class="panel">
          <h2>Intervention Sets</h2>
          <div id="sets" class="sets"></div>
        </section>
        <section class="panel">
          <h2>Replay Comparisons</h2>
          <div id="replays" class="replays"></div>
        </section>
      </main>
    </div>
  </div>
  <script>
    const runsNode = document.querySelector("#runs");
    const summaryNode = document.querySelector("#summary");
    const timelineNode = document.querySelector("#timeline");
    const findingsNode = document.querySelector("#findings");
    const interventionsNode = document.querySelector("#interventions");
    const githubNode = document.querySelector("#github");
    const relatedNode = document.querySelector("#related");
    const exportsNode = document.querySelector("#exports");
    const setsNode = document.querySelector("#sets");
    const replaysNode = document.querySelector("#replays");
    const effectivenessNode = document.querySelector("#effectiveness");
    const actionStatusNode = document.querySelector("#action-status");
    const replayCommandNode = document.querySelector("#replay-command");
    const exportButton = document.querySelector("#export-btn");
    const applyButton = document.querySelector("#apply-btn");
    const replayButton = document.querySelector("#replay-btn");
    const attemptRepairButton = document.querySelector("#attempt-repair-btn");

    let activeRunId = null;

    function escapeHtml(text) {
      return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    }

    async function loadRuns() {
      await loadSummary();
      const response = await fetch("/api/runs");
      const runs = await response.json();
      runsNode.innerHTML = runs.map((run) => `
        <button class="run ${run.id === activeRunId ? "active" : ""}" data-run-id="${run.id}">
          <strong>${escapeHtml(run.command)}</strong>
          <div class="meta">${run.status} · exit ${run.exit_code ?? "-"}</div>
          <div class="small">${escapeHtml(run.created_at)}</div>
        </button>
      `).join("");
      runsNode.querySelectorAll(".run").forEach((node) => {
        node.addEventListener("click", () => {
          activeRunId = node.dataset.runId;
          loadRuns();
          loadRun(activeRunId);
        });
      });
      if (!activeRunId && runs.length) {
        activeRunId = runs[0].id;
        loadRuns();
        loadRun(activeRunId);
      }
    }

    async function loadSummary() {
      const response = await fetch("/api/summary");
      const payload = await response.json();
      const effectiveness = payload.effectiveness;
      effectivenessNode.innerHTML = `
        <div class="item">
          <strong>${escapeHtml(String(effectiveness.total_replays))} replays</strong>
          <div class="meta">improved ${escapeHtml(String(effectiveness.improved_replays))} · avg score ${escapeHtml(String(effectiveness.average_score))}</div>
          <pre>${escapeHtml(effectiveness.type_lines.join("\n"))}</pre>
        </div>
      `;
    }

    async function loadRun(runId) {
      const response = await fetch(`/api/runs/${runId}`);
      const payload = await response.json();
      summaryNode.innerHTML = `
        <strong>${escapeHtml(payload.run.command)}</strong>
        <div class="meta">${payload.run.status} · exit ${payload.run.exit_code ?? "-"}</div>
        <div class="small">${escapeHtml(payload.run.summary ?? "")}</div>
      `;
      timelineNode.innerHTML = payload.events.map((event) => `
        <div class="item">
          <strong>${escapeHtml(event.event_type)}</strong>
          <div class="meta">${escapeHtml(event.timestamp)}</div>
          <pre>${escapeHtml(JSON.stringify(event.payload, null, 2))}</pre>
        </div>
      `).join("");
      findingsNode.innerHTML = payload.findings.length ? payload.findings.map((finding) => `
        <div class="item">
          <strong>${escapeHtml(finding.title)}</strong>
          <div class="meta">${escapeHtml(finding.severity)}</div>
          <p>${escapeHtml(finding.summary)}</p>
          <pre>${escapeHtml(finding.evidence.join("\\n"))}</pre>
        </div>
      `).join("") : `<div class="item">No patterns detected for this run.</div>`;
      interventionsNode.innerHTML = payload.interventions.length ? payload.interventions.map((intervention) => `
        <div class="item">
          <strong>${escapeHtml(intervention.title)}</strong>
          <div class="meta">${escapeHtml(intervention.type)} → ${escapeHtml(intervention.target)}</div>
          <pre>${escapeHtml(intervention.content)}</pre>
        </div>
      `).join("") : `<div class="item">No intervention generated.</div>`;
      githubNode.innerHTML = payload.github ? `
        <div class="item">
          <strong>${escapeHtml(payload.github.repo || "No repo")}</strong>
          <div class="meta">PR ${escapeHtml(String(payload.github.pr_number || "-"))} · ${escapeHtml(payload.github.review_decision || "no review decision")}</div>
          <pre>${escapeHtml(payload.github.summary_lines.join("\\n"))}</pre>
        </div>
      ` : `<div class="item">No GitHub context captured for this run.</div>`;
      relatedNode.innerHTML = payload.related_runs.length ? payload.related_runs.map((run) => `
        <div class="item">
          <strong>${escapeHtml(run.id)}</strong>
          <div class="meta">${escapeHtml(run.status)} · exit ${escapeHtml(String(run.exit_code ?? "-"))}</div>
          <div class="small">${escapeHtml(run.command)}</div>
          <div class="small">${escapeHtml(run.summary || "")}</div>
        </div>
      `).join("") : `<div class="item">No related runs.</div>`;
      exportsNode.innerHTML = payload.exports.length ? payload.exports.map((item) => `
        <div class="item">
          <strong>${escapeHtml(item.label)}</strong>
          <pre>${escapeHtml(item.path)}</pre>
        </div>
      `).join("") : `<div class="item">No exported or applied files yet.</div>`;
      setsNode.innerHTML = payload.intervention_sets.length ? payload.intervention_sets.map((item) => `
        <div class="item">
          <strong>v${escapeHtml(String(item.version))} · ${escapeHtml(item.kind)}</strong>
          <div class="meta">set ${escapeHtml(item.id)} · ${escapeHtml(item.created_at)}</div>
          <div class="small">applied: ${escapeHtml(item.applied_at || "no")} · superseded: ${escapeHtml(item.superseded_at || "no")}</div>
          <div class="small">adapter: ${escapeHtml(item.runner_adapter || "shell")}</div>
          <div class="small">context: ${escapeHtml(item.repo || "unknown repo")}${item.pr_number ? `#${escapeHtml(String(item.pr_number))}` : ""}</div>
          <div class="small">scopes: ${escapeHtml((item.scopes || []).join(", ") || "none")}</div>
          <div class="small">targets: ${escapeHtml((item.instruction_targets || []).join(", ") || "none")}</div>
        </div>
      `).join("") : `<div class="item">No intervention sets.</div>`;
      const replayRows = payload.source_replay ? [payload.source_replay] : payload.replays;
      replaysNode.innerHTML = replayRows.length ? replayRows.map((item) => `
        <div class="item">
          <strong>${escapeHtml(item.label)}</strong>
          <div class="meta">source ${escapeHtml(item.source_run_id)} → replay ${escapeHtml(item.replay_run_id)}</div>
          <div class="compare">
            <div>
              <strong>Source</strong>
              <pre>${escapeHtml(item.source_lines.join("\n"))}</pre>
            </div>
            <div>
              <strong>Replay</strong>
              <pre>${escapeHtml(item.replay_lines.join("\n"))}</pre>
            </div>
          </div>
          <pre>${escapeHtml(item.summary_lines.join("\n"))}</pre>
        </div>
      `).join("") : `<div class="item">No replay comparisons yet.</div>`;
    }

    async function runAction(action) {
      if (!activeRunId) return;
      actionStatusNode.textContent = `Running ${action}...`;
      const payload = {};
      if (action === "replay") {
        if (replayCommandNode.value.trim()) payload.command = replayCommandNode.value.trim();
      }
      const response = await fetch(`/api/runs/${activeRunId}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        actionStatusNode.textContent = data.error || `${action} failed`;
        return;
      }
      if (action === "replay" && data.run_id) {
        activeRunId = data.run_id;
      }
      actionStatusNode.textContent = `${action} complete`;
      await loadRuns();
      await loadRun(activeRunId);
    }

    exportButton.addEventListener("click", () => runAction("export"));
    applyButton.addEventListener("click", () => runAction("apply"));
    replayButton.addEventListener("click", () => runAction("replay"));
    attemptRepairButton.addEventListener("click", () => runAction("attempt-repair"));

    loadRuns();
  </script>
</body>
</html>"""


def summarize_github_context(gh_context: dict) -> dict | None:
    if not gh_context:
        return None
    checks = gh_context.get("checks", [])
    ci_runs = gh_context.get("ci_runs", [])
    review_summary = gh_context.get("review_summary", {})
    failing_checks = [check.get("name") for check in checks if (check.get("bucket") or "").lower() == "fail"]
    pending_checks = [check.get("name") for check in checks if (check.get("bucket") or "").lower() == "pending"]
    summary_lines = [
        f"Unresolved threads: {review_summary.get('unresolved_thread_count', 0)}",
        f"Failing checks: {', '.join(failing_checks) or 'none'}",
        f"Pending checks: {', '.join(pending_checks) or 'none'}",
        f"Workflow runs captured: {len(ci_runs)}",
    ]
    for run in ci_runs[:2]:
        summary_lines.append(
            f"{run.get('workflow_name') or 'workflow'}: {run.get('conclusion') or run.get('status')}"
        )
        for line in run.get("failed_log_excerpt", [])[:2]:
            summary_lines.append(f"  {line}")
    return {
        "repo": gh_context.get("repo"),
        "pr_number": gh_context.get("pr_number"),
        "review_decision": gh_context.get("review_decision"),
        "summary_lines": summary_lines,
    }


def summarize_related_run(context: dict) -> dict:
    run = context["run"]
    return {
        "id": run.id,
        "command": run.command,
        "status": run.status,
        "exit_code": run.exit_code,
        "summary": run.summary,
    }


def summarize_output_paths(store: Store, run_id: str) -> list[dict]:
    candidates = [
        ("exports", store.paths.exports_dir / run_id),
        ("applied", store.paths.applied_dir / run_id),
        ("artifacts", store.paths.artifacts_dir / run_id),
    ]
    outputs = []
    for label, path in candidates:
        if path.exists():
            outputs.append({"label": label, "path": str(path)})
    replay_root = store.paths.replays_dir / run_id
    if replay_root.exists():
        outputs.append({"label": "replays", "path": str(replay_root)})
    return outputs


def summarize_intervention_sets(store: Store, run_id: str) -> list[dict]:
    rows = store.list_intervention_sets_for_run(run_id)
    items = []
    for row in rows:
        manifest = json.loads(row["manifest_json"])
        items.append(
            {
                "id": row["id"],
                "version": row["version"],
                "kind": row["kind"],
                "created_at": row["created_at"],
                "applied_at": row["applied_at"],
                "superseded_at": row["superseded_at"],
                "instruction_targets": manifest.get("instruction_targets", []),
                "runner_adapter": manifest.get("runner_adapter", "shell"),
                "repo": manifest.get("context", {}).get("repo"),
                "pr_number": manifest.get("context", {}).get("pr_number"),
                "scopes": sorted(
                    {
                        intervention.get("scope", "pr")
                        for intervention in manifest.get("interventions", [])
                    }
                ),
            }
        )
    return items


def summarize_replays(store: Store, run_id: str) -> list[dict]:
    rows = store.list_replay_runs_for_source(run_id)
    return [format_replay_row(row, "Replay outcome") for row in rows]


def summarize_source_replay(store: Store, run_id: str) -> dict | None:
    row = store.get_replay_source_for_run(run_id)
    if not row:
        return None
    return format_replay_row(row, "Current run replayed from source")


def format_replay_row(row, label: str) -> dict:
    comparison = json.loads(row["comparison_json"])
    manifest = json.loads(row["intervention_manifest_json"])
    lines = [
        f"Adapter: {manifest.get('runner_adapter', 'shell')}",
        f"Verdict: {comparison.get('verdict')} (score {comparison.get('score')})",
        f"Improved: {comparison.get('improved')}",
        f"Status: {comparison.get('source_status')} -> {comparison.get('replay_status')}",
        f"Exit: {comparison.get('source_exit_code')} -> {comparison.get('replay_exit_code')}",
        f"Findings: {comparison.get('source_findings')} -> {comparison.get('replay_findings')}",
        f"Failing checks: {comparison.get('source_failing_checks')} -> {comparison.get('replay_failing_checks')}",
        f"Unresolved review files: {len(comparison.get('source_unresolved_review_files', []))} -> {len(comparison.get('replay_unresolved_review_files', []))}",
        f"Failure files: {len(comparison.get('source_failure_files', []))} -> {len(comparison.get('replay_failure_files', []))}",
        f"Failure-surface overlap: {comparison.get('source_overlap_count')} -> {comparison.get('replay_overlap_count')}",
        f"Instruction targets: {', '.join(manifest.get('instruction_targets', [])) or 'none'}",
    ]
    if comparison.get("resolved_findings"):
        lines.append(f"Resolved findings: {', '.join(comparison['resolved_findings'])}")
    if comparison.get("persisted_findings"):
        lines.append(f"Persisted findings: {', '.join(comparison['persisted_findings'])}")
    if comparison.get("new_findings"):
        lines.append(f"New findings: {', '.join(comparison['new_findings'])}")
    return {
        "label": label,
        "source_run_id": row["source_run_id"],
        "replay_run_id": row["replay_run_id"],
        "source_lines": [
            f"status: {comparison.get('source_status')}",
            f"exit: {comparison.get('source_exit_code')}",
            f"findings: {comparison.get('source_findings')}",
            f"failing checks: {comparison.get('source_failing_checks')}",
            f"failure files: {', '.join(comparison.get('source_failure_files', [])) or 'none'}",
        ],
        "replay_lines": [
            f"status: {comparison.get('replay_status')}",
            f"exit: {comparison.get('replay_exit_code')}",
            f"findings: {comparison.get('replay_findings')}",
            f"failing checks: {comparison.get('replay_failing_checks')}",
            f"failure files: {', '.join(comparison.get('replay_failure_files', [])) or 'none'}",
        ],
        "summary_lines": lines,
    }


def summarize_effectiveness(store: Store) -> dict:
    rows = store.list_all_replay_runs()
    if not rows:
        return {
            "total_replays": 0,
            "improved_replays": 0,
            "average_score": 0,
            "type_lines": ["No replay attempts recorded yet."],
        }

    total_score = 0
    improved_replays = 0
    by_type: dict[str, dict[str, float]] = {}
    resolved_counts: dict[str, int] = {}
    for row in rows:
        comparison = json.loads(row["comparison_json"])
        manifest = json.loads(row["intervention_manifest_json"])
        score = float(comparison.get("score", 0))
        total_score += score
        if comparison.get("improved"):
            improved_replays += 1
        types = {
            intervention["type"]
            for intervention in manifest.get("interventions", [])
        }
        for intervention_type in types:
            bucket = by_type.setdefault(
                intervention_type,
                {"attempts": 0, "improved": 0, "score_total": 0.0, "resolved_total": 0.0},
            )
            bucket["attempts"] += 1
            bucket["score_total"] += score
            bucket["resolved_total"] += len(comparison.get("resolved_findings", []))
            if comparison.get("improved"):
                bucket["improved"] += 1
        for code in comparison.get("resolved_findings", []):
            resolved_counts[code] = resolved_counts.get(code, 0) + 1

    type_lines = []
    for intervention_type in sorted(by_type):
        bucket = by_type[intervention_type]
        avg_score = bucket["score_total"] / bucket["attempts"]
        win_rate = (bucket["improved"] / bucket["attempts"]) * 100
        avg_resolved = bucket["resolved_total"] / bucket["attempts"]
        type_lines.append(
            f"{intervention_type}: attempts={int(bucket['attempts'])} improved={int(bucket['improved'])} win_rate={win_rate:.0f}% avg_score={avg_score:.1f} avg_resolved={avg_resolved:.1f}"
        )
    top_resolved = [
        f"{code}={count}"
        for code, count in sorted(resolved_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    return {
        "total_replays": len(rows),
        "improved_replays": improved_replays,
        "average_score": round(total_score / len(rows), 1),
        "type_lines": type_lines + ([f"top_resolved: {', '.join(top_resolved)}"] if top_resolved else []),
    }
