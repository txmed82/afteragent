from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import runner_adapter_names
from .capture import run_command, validate_github_pr
from .config import resolve_paths
from .diagnostics import analyze_run
from .llm.config import load_config
from .llm.client import get_client
from .llm.enhancer import enhance_diagnosis_with_llm
from .mcp_server import serve_stdio as serve_mcp_stdio
from .session import approve_actions, finalize_run
from .store import Store
from .ui import serve
from .workflow import apply_interventions, attempt_repair, export_interventions, replay_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="afteragent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    exec_parser = subparsers.add_parser("exec", help="Capture a command run")
    exec_parser.add_argument("--summary", help="Optional run summary")
    exec_parser.add_argument("--github-repo", help="Override GitHub repo for PR context capture")
    exec_parser.add_argument("--github-pr", type=int, help="Override GitHub PR number for context capture")
    exec_parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Do not mirror stdout/stderr live while capturing",
    )
    enhance_group = exec_parser.add_mutually_exclusive_group()
    enhance_group.add_argument(
        "--enhance",
        dest="enhance",
        action="store_true",
        default=None,
        help="Force LLM enhancement after the run, overriding config.",
    )
    enhance_group.add_argument(
        "--no-enhance",
        dest="enhance",
        action="store_false",
        default=None,
        help="Skip LLM enhancement for this run, overriding config.",
    )
    exec_parser.add_argument(
        "--task",
        dest="task_prompt",
        help="Override the auto-detected task prompt with an explicit string",
    )
    exec_parser.add_argument("cmd", nargs=argparse.REMAINDER)

    subparsers.add_parser("runs", help="List captured runs")

    show_parser = subparsers.add_parser("show", help="Show run details")
    show_parser.add_argument("run_id")

    diagnose_parser = subparsers.add_parser("diagnose", help="Analyze a run")
    diagnose_parser.add_argument("run_id")

    export_parser = subparsers.add_parser(
        "export-interventions", help="Export interventions for a run into files"
    )
    export_parser.add_argument("run_id")
    export_parser.add_argument("--output-dir", help="Optional output directory")

    apply_parser = subparsers.add_parser(
        "apply-interventions", help="Apply interventions for a run to the local workspace"
    )
    apply_parser.add_argument("run_id")

    finalize_parser = subparsers.add_parser(
        "finalize", help="Finalize an MCP-native run and render findings, actions, and compression data"
    )
    finalize_parser.add_argument("run_id")

    approve_parser = subparsers.add_parser(
        "approve", help="Approve and execute pending actions for a run"
    )
    approve_parser.add_argument("run_id")
    approve_parser.add_argument("--action-id", dest="action_ids", type=int, action="append")

    replay_parser = subparsers.add_parser(
        "replay", help="Fork a prior run with exported intervention context"
    )
    replay_parser.add_argument("run_id", help="Source run to replay from")
    replay_parser.add_argument("--summary", help="Optional replay summary")
    replay_parser.add_argument(
        "--apply-interventions",
        action="store_true",
        help="Apply interventions to the workspace before replaying",
    )
    replay_parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Do not mirror stdout/stderr live while replaying",
    )
    replay_parser.add_argument(
        "--runner",
        choices=runner_adapter_names(),
        help="Optional runner preset to force adapter selection",
    )
    replay_parser.add_argument("cmd", nargs=argparse.REMAINDER)

    attempt_parser = subparsers.add_parser(
        "attempt-repair", help="Validate PR state, apply interventions, and launch a replay in one step"
    )
    source_group = attempt_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--run-id", help="Use an existing captured run as the repair source")
    source_group.add_argument("--repo", help="GitHub repo in owner/name form for a fresh validation capture")
    attempt_parser.add_argument("--pr", type=int, help="Pull request number when using --repo")
    attempt_parser.add_argument("--summary", help="Optional replay summary")
    attempt_parser.add_argument(
        "--runner",
        choices=runner_adapter_names(),
        help="Optional runner preset to force adapter selection",
    )
    attempt_parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Do not mirror stdout/stderr live while running the repair attempt",
    )
    attempt_parser.add_argument("cmd", nargs=argparse.REMAINDER)

    validate_parser = subparsers.add_parser(
        "validate-pr", help="Capture GitHub context for an explicit repo/PR"
    )
    validate_parser.add_argument("--repo", required=True, help="GitHub repo in owner/name form")
    validate_parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    validate_parser.add_argument("--summary", help="Optional run summary")

    ui_parser = subparsers.add_parser("ui", help="Serve the local viewer")
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=8765)

    server_parser = subparsers.add_parser("server", help="Serve AfterAgent as a local MCP stdio server")
    server_parser.add_argument(
        "--stdio",
        action="store_true",
        default=True,
        help="Serve over stdio using the MCP JSON-RPC framing (default: true).",
    )

    enhance_parser = subparsers.add_parser(
        "enhance", help="Run LLM-driven diagnosis enhancement on a captured run"
    )
    enhance_parser.add_argument("run_id", help="Run ID to enhance")
    enhance_parser.add_argument(
        "--llm-provider",
        help="Override LLM provider (anthropic | openai | openrouter | ollama)",
    )
    enhance_parser.add_argument("--llm-model", help="Override LLM model name")
    enhance_parser.add_argument("--llm-base-url", help="Override LLM base URL")

    stats_parser = subparsers.add_parser(
        "stats",
        help="Show effectiveness metrics aggregated from replay history",
    )
    stats_parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum samples required before a metric is included (default: 5)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(resolve_paths())

    if args.command == "exec":
        command = args.cmd
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("afteragent exec requires a command after --")
        if args.github_repo or args.github_pr:
            result = run_command(
                store,
                command,
                Path.cwd(),
                summary=args.summary,
                github_repo=args.github_repo,
                github_pr=args.github_pr,
                stream_output=not args.no_stream,
                task_prompt=getattr(args, "task_prompt", None),  # NEW
            )
        else:
            result = run_command(
                store,
                command,
                Path.cwd(),
                summary=args.summary,
                stream_output=not args.no_stream,
                task_prompt=getattr(args, "task_prompt", None),  # NEW
            )
        run_id = str(result["run_id"])
        findings, interventions = analyze_run(store, run_id)
        print(f"captured run {run_id}")
        _print_analysis(findings, interventions)

        # Decide whether to auto-enhance. Precedence:
        # 1. CLI flag (args.enhance is not None)
        # 2. Config file (auto_enhance_on_exec)
        # 3. Default: no enhancement
        should_enhance: bool | None = getattr(args, "enhance", None)
        if should_enhance is None:
            config = load_config(store.paths)
            should_enhance = bool(config and config.auto_enhance_on_exec)

        if should_enhance:
            config = load_config(store.paths)
            if config is None:
                print(
                    "  (enhance requested but no LLM provider configured — skipping)"
                )
            else:
                try:
                    client = get_client(config)
                    enhance_result = enhance_diagnosis_with_llm(
                        store, run_id, client, config,
                    )
                    cost_str = (
                        f"${enhance_result.total_cost_usd:.4f}"
                        if enhance_result.total_cost_usd > 0
                        else "free"
                    )
                    print(
                        f"  enhanced: +{enhance_result.findings_count} findings, "
                        f"{enhance_result.interventions_count} intervention(s) "
                        f"({cost_str})"
                    )
                except ImportError as exc:
                    print(f"  (LLM enhancement skipped: {exc})")

        return int(result["exit_code"])

    if args.command == "runs":
        for run in store.list_runs():
            print(
                f"{run.id}\t{run.status}/{run.lifecycle_status}\texit={run.exit_code}\t"
                f"{run.created_at}\t{run.command}"
            )
        return 0

    if args.command == "show":
        run = store.get_run(args.run_id)
        if not run:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        print(f"Run {run.id}")
        print(f"  command: {run.command}")
        print(f"  cwd: {run.cwd}")
        print(f"  status: {run.status}")
        print(f"  exit_code: {run.exit_code}")
        print(f"  created_at: {run.created_at}")
        print(f"  duration_ms: {run.duration_ms}")
        print(f"  summary: {run.summary}")
        print(f"  client_name: {run.client_name}")
        print(f"  lifecycle_status: {run.lifecycle_status}")
        print(f"  finalized_at: {run.finalized_at}")
        print("  events:")
        for event in store.get_events(run.id):
            payload = json.dumps(json.loads(event.payload_json), sort_keys=True)
            print(f"    - {event.timestamp} {event.event_type} {payload}")
        pending_actions = store.list_pending_actions(run.id)
        if pending_actions:
            print("  pending_actions:")
            for action in pending_actions:
                print(f"    - {action.id} {action.action_type} {action.status} {action.title}")
        return 0

    if args.command == "diagnose":
        run = store.get_run(args.run_id)
        if not run:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        findings, interventions = analyze_run(store, args.run_id)
        _print_analysis(findings, interventions)
        return 0

    if args.command == "export-interventions":
        manifest = export_interventions(
            store,
            args.run_id,
            base_dir=Path.cwd(),
            output_dir=Path(args.output_dir) if args.output_dir else None,
        )
        print(f"exported interventions for {args.run_id}")
        print(json.dumps(manifest, indent=2))
        return 0

    if args.command == "apply-interventions":
        manifest = apply_interventions(store, args.run_id, Path.cwd())
        print(f"applied interventions for {args.run_id}")
        print(json.dumps(manifest, indent=2))
        return 0

    if args.command == "finalize":
        result = finalize_run(store, args.run_id)
        print(f"finalized run {args.run_id}")
        _print_analysis(
            _finding_objects_from_result(result["findings"]),
            _intervention_objects_from_result(result["interventions"]),
        )
        _print_recommendations(result["recommendations"])
        _print_pending_actions(result["pending_actions"])
        _print_compression_report(result["compression_report"])
        return 0

    if args.command == "approve":
        run = store.get_run(args.run_id)
        if not run:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        run_cwd = Path(run.cwd)
        results = approve_actions(store, args.run_id, run_cwd, args.action_ids)
        print(json.dumps({"run_id": args.run_id, "results": results}, indent=2))
        return 0

    if args.command == "replay":
        summary, apply_interventions_first, no_stream, command = normalize_replay_args(
            args.summary,
            args.apply_interventions,
            args.no_stream,
            args.cmd,
        )
        result = replay_run(
            store,
            source_run_id=args.run_id,
            cwd=Path.cwd(),
            command=command or None,
            summary=summary,
            apply_interventions_first=apply_interventions_first,
            stream_output=not no_stream,
            runner=args.runner,
        )
        run_id = str(result["run_id"])
        findings, interventions = analyze_run(store, run_id)
        print(f"captured run {run_id}")
        _print_analysis(findings, interventions)
        return int(result["exit_code"])

    if args.command == "attempt-repair":
        command = args.cmd
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("afteragent attempt-repair requires a command after --")
        if args.repo and args.pr is None:
            parser.error("afteragent attempt-repair requires --pr when using --repo")
        result = attempt_repair(
            store,
            cwd=Path.cwd(),
            command=command,
            source_run_id=args.run_id,
            repo=args.repo,
            pr_number=args.pr,
            summary=args.summary,
            stream_output=not args.no_stream,
            runner=args.runner,
        )
        replay_run_id = str(result["replay_run_id"])
        findings, interventions = analyze_run(store, replay_run_id)
        print(json.dumps(result, indent=2))
        _print_analysis(findings, interventions)
        return int(result["exit_code"])

    if args.command == "validate-pr":
        result = validate_github_pr(
            store,
            repo=args.repo,
            pr_number=args.pr,
            cwd=Path.cwd(),
            summary=args.summary,
        )
        run_id = str(result["run_id"])
        findings, interventions = analyze_run(store, run_id)
        print(f"captured run {run_id}")
        _print_analysis(findings, interventions)
        return 0

    if args.command == "ui":
        try:
            serve(store, host=args.host, port=args.port)
        except KeyboardInterrupt:
            print("\nUI stopped.")
            return 130
        return 0

    if args.command == "server":
        return serve_mcp_stdio(store, Path.cwd())

    if args.command == "enhance":
        cli_overrides = {}
        if args.llm_provider:
            cli_overrides["provider"] = args.llm_provider
        if args.llm_model:
            cli_overrides["model"] = args.llm_model
        if args.llm_base_url:
            cli_overrides["base_url"] = args.llm_base_url

        config = load_config(store.paths, cli_overrides=cli_overrides or None)
        if config is None:
            print(
                "No LLM provider configured. Set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
                "OPENROUTER_API_KEY / OLLAMA_BASE_URL, or create .afteragent/config.toml. "
                "See `afteragent enhance --help`."
            )
            return 1

        try:
            client = get_client(config)
        except ImportError as exc:
            print(f"Cannot instantiate LLM client: {exc}")
            return 1

        result = enhance_diagnosis_with_llm(store, args.run_id, client, config)
        cost_str = f"${result.total_cost_usd:.4f}" if result.total_cost_usd > 0 else "free"
        print(
            f"Enhanced run {args.run_id}: "
            f"+{result.findings_count} findings, "
            f"{result.interventions_count} intervention(s) "
            f"({result.total_input_tokens} in / {result.total_output_tokens} out tokens, "
            f"{cost_str})"
        )
        if result.error_messages:
            for err in result.error_messages:
                print(f"  warning: {err}")
        return 0 if result.status != "error" else 1

    if args.command == "stats":
        from .effectiveness import (
            compute_effectiveness_metrics,
            format_metrics_for_cli,
        )
        report = compute_effectiveness_metrics(store, min_samples=args.min_samples)
        print(format_metrics_for_cli(report))
        return 0

    parser.error(f"Unhandled command: {args.command}")
    return 2


def _print_analysis(findings: list, interventions: list) -> None:
    if findings:
        print("findings:")
        for finding in findings:
            print(f"- [{finding.severity}] {finding.title}")
            print(f"  {finding.summary}")
            for evidence in finding.evidence:
                print(f"  evidence: {evidence}")
    else:
        print("findings:\n- none")

    if interventions:
        print("interventions:")
        for intervention in interventions:
            print(f"- {intervention.type} -> {intervention.target}")
            print(f"  {intervention.title}")
            print(f"  {intervention.content}")
    else:
        print("interventions:\n- none")

    print()


def _print_recommendations(items: list[dict]) -> None:
    if not items:
        print("recommendations:\n- none\n")
        return
    print("recommendations:")
    for item in items:
        print(f"- [{item['kind']}] {item['title']}")
        print(f"  {item['rationale']}")
        if item.get("install_command"):
            print(f"  install: {' '.join(item['install_command'])}")
    print()


def _print_pending_actions(items: list[dict]) -> None:
    if not items:
        print("pending actions:\n- none\n")
        return
    print("pending actions:")
    for item in items:
        print(f"- #{item['id']} {item['type']} [{item['status']}]")
        print(f"  {item['title']}")
    print()


def _print_compression_report(items: list[dict]) -> None:
    if not items:
        print("compression:\n- none\n")
        return
    print("compression:")
    for item in items:
        print(
            f"- {item['artifact_kind']} {item['strategy']} "
            f"{item['original_size']} -> {item['compressed_size']} chars"
        )
    print()


def _finding_objects_from_result(items: list[dict]) -> list:
    class Finding:
        def __init__(self, payload: dict):
            self.severity = payload["severity"]
            self.title = payload["title"]
            self.summary = payload["summary"]
            self.evidence = payload["evidence"]
    return [Finding(item) for item in items]


def _intervention_objects_from_result(items: list[dict]) -> list:
    class Intervention:
        def __init__(self, payload: dict):
            self.type = payload["type"]
            self.target = payload["target"]
            self.title = payload["title"]
            self.content = payload["content"]
    return [Intervention(item) for item in items]


def normalize_replay_args(
    summary: str | None,
    apply_interventions: bool,
    no_stream: bool,
    command: list[str],
) -> tuple[str | None, bool, bool, list[str]]:
    normalized_summary = summary
    normalized_apply = apply_interventions
    normalized_no_stream = no_stream
    remaining = list(command)
    while remaining and remaining[0] != "--":
        token = remaining[0]
        if token == "--summary" and len(remaining) >= 2:
            normalized_summary = remaining[1]
            remaining = remaining[2:]
            continue
        if token == "--apply-interventions":
            normalized_apply = True
            remaining = remaining[1:]
            continue
        if token == "--no-stream":
            normalized_no_stream = True
            remaining = remaining[1:]
            continue
        break
    if remaining and remaining[0] == "--":
        remaining = remaining[1:]
    return normalized_summary, normalized_apply, normalized_no_stream, remaining


if __name__ == "__main__":
    raise SystemExit(main())