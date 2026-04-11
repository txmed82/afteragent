from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from .transcripts import TranscriptEvent, parse_generic_stdout

KNOWN_INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "CURSOR.md",
    "COPILOT.md",
)


@dataclass(frozen=True)
class RunnerLaunchPlan:
    adapter_name: str
    command: list[str]
    env: dict[str, str]
    instruction_targets: list[Path]


class RunnerAdapter:
    name = "shell"
    command_names: tuple[str, ...] = ()
    instruction_files: tuple[str, ...] = ()

    def detect(
        self,
        cwd: Path,
        command: list[str] | None = None,
        source_command: str | None = None,
    ) -> bool:
        del cwd
        return self._matches_command(command) or self._matches_source_command(source_command)

    def instruction_targets(self, cwd: Path) -> list[Path]:
        existing = [cwd / name for name in self.instruction_files if (cwd / name).exists()]
        return existing or [cwd / self.default_instruction_file()]

    def default_instruction_file(self) -> str:
        if self.instruction_files:
            return self.instruction_files[0]
        return KNOWN_INSTRUCTION_FILES[0]

    def launch(
        self,
        cwd: Path,
        command: list[str],
        extra_env: dict[str, str],
    ) -> RunnerLaunchPlan:
        instruction_targets = self.instruction_targets(cwd)
        env = {
            **extra_env,
            "AFTERACTION_RUNNER_ADAPTER": self.name,
            "AFTERACTION_INSTRUCTION_TARGETS": ":".join(path.name for path in instruction_targets),
            "AFTERACTION_PRIMARY_INSTRUCTION_PATH": str(instruction_targets[0]),
            "AFTERACTION_REPO_INSTRUCTION_PATHS": os.pathsep.join(str(path) for path in instruction_targets),
        }
        return RunnerLaunchPlan(
            adapter_name=self.name,
            command=command,
            env=env,
            instruction_targets=instruction_targets,
        )

    def transcript_event_patterns(self) -> list[tuple[str, re.Pattern[str], str]]:
        return []

    def transcript_file_globs(self) -> tuple[str, ...]:
        return ()

    def parse_transcript_events(
        self,
        stdout_text: str,
        stderr_text: str,
        artifact_dir: Path,
    ) -> list[dict]:
        events: list[dict] = []
        events.extend(self._parse_pattern_events(stdout_text, source="stdout"))
        events.extend(self._parse_pattern_events(stderr_text, source="stderr"))
        for path in self._transcript_files(artifact_dir):
            try:
                text = path.read_text()
            except OSError:
                continue
            events.extend(self._parse_pattern_events(text, source=path.name))
        return dedupe_events(events)

    def pre_launch_snapshot(self, cwd: Path) -> dict:
        """Snapshot runner-specific pre-launch state (e.g. transcript directory).

        Called by capture.run_command before subprocess.Popen. The returned
        dict is passed back into parse_transcript after the subprocess exits.
        Default implementation returns an empty dict.
        """
        del cwd
        return {}

    def parse_transcript(
        self,
        run_id: str,
        artifact_dir: Path,
        stdout: str,
        stderr: str,
        pre_launch_state: dict,
    ) -> list[TranscriptEvent]:
        """Parse the runner's transcript into normalized TranscriptEvent objects.

        Default implementation uses the generic stdout heuristic parser.
        Runner subclasses override to provide richer parsing.
        Must never raise — all failures become parse_error events.
        """
        del artifact_dir, pre_launch_state
        return parse_generic_stdout(run_id=run_id, stdout=stdout, stderr=stderr)

    def _parse_pattern_events(self, text: str, source: str) -> list[dict]:
        events = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            for event_type, pattern, field_name in self.transcript_event_patterns():
                match = pattern.search(stripped)
                if not match:
                    continue
                payload = {
                    "runner_adapter": self.name,
                    "source": source,
                    "line": stripped,
                }
                value = match.group(field_name).strip()
                if event_type == "tool.called":
                    payload["tool"] = value
                elif event_type == "file.edited":
                    payload["path"] = value
                elif event_type == "retry.detected":
                    payload["attempt"] = value
                events.append({"event_type": event_type, "payload": payload})
                break
        return events

    def _transcript_files(self, artifact_dir: Path) -> list[Path]:
        paths: list[Path] = []
        for pattern in self.transcript_file_globs():
            paths.extend(sorted(artifact_dir.glob(pattern)))
        return paths

    def _matches_command(self, command: list[str] | None) -> bool:
        if not command:
            return False
        executable = Path(command[0]).name.lower()
        return executable in self.command_names

    def _matches_source_command(self, source_command: str | None) -> bool:
        if not source_command:
            return False
        try:
            parsed = shlex.split(source_command)
        except ValueError:
            return False
        return self._matches_command(parsed)


class ShellAdapter(RunnerAdapter):
    name = "shell"
    instruction_files = KNOWN_INSTRUCTION_FILES

    def detect(
        self,
        cwd: Path,
        command: list[str] | None = None,
        source_command: str | None = None,
    ) -> bool:
        del cwd, command, source_command
        return True

    def instruction_targets(self, cwd: Path) -> list[Path]:
        existing = [cwd / name for name in KNOWN_INSTRUCTION_FILES if (cwd / name).exists()]
        return existing or [cwd / self.default_instruction_file()]

    def transcript_event_patterns(self) -> list[tuple[str, re.Pattern[str], str]]:
        return [
            ("tool.called", re.compile(r"^(?:tool(?:\s+call)?|using tool)\s*[:=-]\s*(?P<tool>[A-Za-z0-9_.-]+)", re.I), "tool"),
            ("file.edited", re.compile(r"^(?:edited|updated|created|wrote)\s+(?P<path>[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", re.I), "path"),
            ("retry.detected", re.compile(r"^(?:retrying|retry attempt)\s*#?(?P<attempt>[0-9]+)?", re.I), "attempt"),
        ]


class ClaudeCodeAdapter(RunnerAdapter):
    name = "claude-code"
    command_names = ("claude", "claude-code")
    instruction_files = ("CLAUDE.md",)

    def detect(
        self,
        cwd: Path,
        command: list[str] | None = None,
        source_command: str | None = None,
    ) -> bool:
        if command or source_command:
            return super().detect(cwd, command, source_command)
        return (cwd / "CLAUDE.md").exists()

    def transcript_event_patterns(self) -> list[tuple[str, re.Pattern[str], str]]:
        return [
            ("tool.called", re.compile(r"^(?:tool(?:\s+use|\s+call)?|using tool)\s*[:=-]\s*(?P<tool>[A-Za-z0-9_.-]+)", re.I), "tool"),
            ("file.edited", re.compile(r"^(?:edited|updated|created)\s+(?P<path>[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", re.I), "path"),
            ("retry.detected", re.compile(r"^(?:retrying|attempt)\s*#?(?P<attempt>[0-9]+)", re.I), "attempt"),
        ]

    def transcript_file_globs(self) -> tuple[str, ...]:
        return ("claude*.log", "claude*.jsonl")

    def pre_launch_snapshot(self, cwd: Path) -> dict:
        slug = claude_project_slug(cwd)
        project_dir = Path.home() / ".claude" / "projects" / slug
        pre: dict[Path, float] = {}
        if project_dir.exists():
            try:
                for path in project_dir.glob("*.jsonl"):
                    try:
                        pre[path] = path.stat().st_mtime
                    except OSError:
                        continue
            except OSError:
                pass
        return {
            "claude_project_dir": project_dir,
            "pre_jsonl_files": pre,
            "launched_at": time.time(),
        }


class CodexAdapter(RunnerAdapter):
    name = "codex"
    command_names = ("codex",)
    instruction_files = ("AGENTS.md",)

    def detect(
        self,
        cwd: Path,
        command: list[str] | None = None,
        source_command: str | None = None,
    ) -> bool:
        if command or source_command:
            return super().detect(cwd, command, source_command)
        return (cwd / "AGENTS.md").exists()

    def transcript_event_patterns(self) -> list[tuple[str, re.Pattern[str], str]]:
        return [
            ("tool.called", re.compile(r"^(?:tool(?:\s+call)?|invoking tool)\s*[:=-]\s*(?P<tool>[A-Za-z0-9_.-]+)", re.I), "tool"),
            ("file.edited", re.compile(r"^(?:edited|patched|updated)\s+(?P<path>[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", re.I), "path"),
            ("retry.detected", re.compile(r"^(?:retrying|retry)\s*#?(?P<attempt>[0-9]+)", re.I), "attempt"),
        ]

    def transcript_file_globs(self) -> tuple[str, ...]:
        return ("codex*.log", "codex*.jsonl")


class OpenClawAdapter(RunnerAdapter):
    name = "openclaw"
    command_names = ("openclaw",)
    instruction_files = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")

    def transcript_event_patterns(self) -> list[tuple[str, re.Pattern[str], str]]:
        return [
            ("tool.called", re.compile(r"^(?:tool(?:\s+call)?|action)\s*[:=-]\s*(?P<tool>[A-Za-z0-9_.-]+)", re.I), "tool"),
            ("file.edited", re.compile(r"^(?:edited|patched|wrote|upload-file)\s+(?P<path>[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", re.I), "path"),
            ("retry.detected", re.compile(r"^(?:retrying|attempt)\s*#?(?P<attempt>[0-9]+)", re.I), "attempt"),
        ]

    def transcript_file_globs(self) -> tuple[str, ...]:
        return ("openclaw*.log", "openclaw*.jsonl")


def claude_project_slug(cwd: Path) -> str:
    """Compute the Claude Code project-directory slug for a working directory.

    Claude Code stores JSONL transcripts under ~/.claude/projects/<slug>/ where
    <slug> is the absolute cwd path with "/" and " " both replaced by "-".
    Other characters are preserved including case.

    Uses Path.absolute() (not resolve()) so symlinks are NOT followed — this
    matches the path Claude Code itself stores under, which is the literal cwd
    as seen by the invoking process.
    """
    s = str(cwd.absolute())
    return s.replace("/", "-").replace(" ", "-")


def find_candidate_jsonl(
    project_dir: Path,
    pre_jsonl_files: dict[Path, float],
    launched_at: float,
    exit_time: float,
) -> tuple[Path | None, bool]:
    """Identify which JSONL file a Claude Code invocation wrote.

    Returns (chosen_path, ambiguous). ambiguous=True means multiple candidates
    existed and the heuristic picked one — caller should emit a parse_error.
    """
    if not project_dir.exists():
        return (None, False)

    try:
        current = list(project_dir.glob("*.jsonl"))
    except OSError:
        return (None, False)

    candidates: list[Path] = []
    for path in current:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if path not in pre_jsonl_files:
            candidates.append(path)
        elif mtime > pre_jsonl_files[path] and mtime >= launched_at:
            candidates.append(path)

    if not candidates:
        return (None, False)
    if len(candidates) == 1:
        return (candidates[0], False)

    # Multiple candidates: pick the one whose mtime is closest to (but not
    # later than) exit_time + 2s grace.
    grace = exit_time + 2.0
    best: Path | None = None
    best_delta: float | None = None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > grace:
            continue
        delta = abs(grace - mtime)
        if best_delta is None or delta < best_delta:
            best = path
            best_delta = delta
    return (best or candidates[0], True)


ADAPTERS: tuple[RunnerAdapter, ...] = (
    OpenClawAdapter(),
    ClaudeCodeAdapter(),
    CodexAdapter(),
    ShellAdapter(),
)


def select_runner_adapter(
    cwd: Path,
    command: list[str] | None = None,
    source_command: str | None = None,
    preferred: str | None = None,
) -> RunnerAdapter:
    if preferred:
        return get_runner_adapter(preferred)
    for adapter in ADAPTERS:
        if adapter.detect(cwd, command=command, source_command=source_command):
            return adapter
    return ShellAdapter()


def get_runner_adapter(name: str) -> RunnerAdapter:
    normalized = name.lower()
    for adapter in ADAPTERS:
        if adapter.name == normalized:
            return adapter
    raise ValueError(f"Unknown runner adapter: {name}")


def runner_adapter_names() -> list[str]:
    return [adapter.name for adapter in ADAPTERS]


def dedupe_events(events: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for event in events:
        payload = event["payload"]
        signature = (
            event["event_type"],
            payload.get("tool"),
            payload.get("path"),
            payload.get("attempt"),
            payload.get("source"),
            payload.get("line"),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(event)
    return deduped
