from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    db_path: Path
    artifacts_dir: Path
    exports_dir: Path
    applied_dir: Path
    replays_dir: Path


def resolve_paths(base_dir: Path | None = None) -> AppPaths:
    root = (base_dir or Path.cwd()) / ".afteraction"
    return AppPaths(
        root=root,
        db_path=root / "afteraction.sqlite3",
        artifacts_dir=root / "artifacts",
        exports_dir=root / "exports",
        applied_dir=root / "applied",
        replays_dir=root / "replays",
    )
