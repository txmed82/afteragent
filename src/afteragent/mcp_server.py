from __future__ import annotations

from dataclasses import asdict
import json
import sys
from pathlib import Path

from .session import approve_actions, append_events, attach_context, finalize_run, start_run
from .store import Store


def _read_message() -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        try:
            key, _, value = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = value.strip()
        except UnicodeDecodeError:
            # Malformed header line, skip this message
            return None
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        # Invalid Content-Length header
        return None
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Malformed JSON or encoding
        return None


def _write_message(payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _tool_result(data: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=True)}],
        "structuredContent": data,
    }


def _tools() -> list[dict]:
    return [
        {
            "name": "start_run",
            "description": "Start a new AfterAgent MCP-native session.",
            "inputSchema": {
                "type": "object",
                "required": ["task_prompt"],
                "properties": {
                    "task_prompt": {"type": "string"},
                    "cwd": {"type": "string"},
                    "client_name": {"type": "string"},
                    "repo_context": {"type": "object"},
                },
            },
        },
        {
            "name": "append_events",
            "description": "Append normalized run events to an active session.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id", "events"],
                "properties": {
                    "run_id": {"type": "string"},
                    "events": {"type": "array", "items": {"type": "object"}},
                },
            },
        },
        {
            "name": "attach_context",
            "description": "Attach stdout/stderr/git diff or GitHub context to a run.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id", "context"],
                "properties": {
                    "run_id": {"type": "string"},
                    "context": {"type": "object"},
                },
            },
        },
        {
            "name": "finalize_run",
            "description": "Finalize a run, compress context, diagnose it, and return pending actions.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}},
            },
        },
        {
            "name": "approve_actions",
            "description": "Approve and execute pending actions for a run.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id"],
                "properties": {
                    "run_id": {"type": "string"},
                    "cwd": {"type": "string"},
                    "action_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
        {
            "name": "list_runs",
            "description": "List known runs.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "show_run",
            "description": "Show a single run, its pending actions, and memory hits.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}},
            },
        },
    ]


def serve_stdio(store: Store, cwd: Path) -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "afteragent", "version": "0.4.0"},
                    "capabilities": {"tools": {}},
                }
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                result = {"tools": _tools()}
            elif method == "tools/call":
                name = params["name"]
                arguments = params.get("arguments", {})
                if name == "start_run":
                    result = _tool_result(
                        start_run(
                            store,
                            cwd=Path(arguments.get("cwd") or cwd),
                            task_prompt=arguments["task_prompt"],
                            client_name=arguments.get("client_name"),
                            repo_context=arguments.get("repo_context"),
                        )
                    )
                elif name == "append_events":
                    result = _tool_result(append_events(store, arguments["run_id"], arguments["events"]))
                elif name == "attach_context":
                    attach_context(store, arguments["run_id"], arguments["context"])
                    result = _tool_result({"ok": True})
                elif name == "finalize_run":
                    result = _tool_result(finalize_run(store, arguments["run_id"]))
                elif name == "approve_actions":
                    run = store.get_run(arguments["run_id"])
                    run_cwd = Path(run.cwd) if run else Path(arguments.get("cwd") or cwd)
                    result = _tool_result(
                        {
                            "results": approve_actions(
                                store,
                                arguments["run_id"],
                                run_cwd,
                                arguments.get("action_ids"),
                            )
                        }
                    )
                elif name == "list_runs":
                    result = _tool_result({"runs": [asdict(run) for run in store.list_runs()]})
                elif name == "show_run":
                    run = store.get_run(arguments["run_id"])
                    result = _tool_result(
                        {
                            "run": asdict(run) if run else None,
                            "pending_actions": [asdict(action) for action in store.list_pending_actions(arguments["run_id"])],
                            "memory_hits": [dict(row) for row in store.list_memory_hits(arguments["run_id"])],
                        }
                    )
                else:
                    raise ValueError(f"Unknown tool: {name}")
            else:
                raise ValueError(f"Unsupported method: {method}")
            if request_id is not None:
                _write_message({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            if request_id is not None:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": str(exc)},
                    }
                )