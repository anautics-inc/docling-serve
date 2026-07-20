"""Deterministic source-snapshot and Graphify metadata."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

SNAPSHOT_ALGORITHM = "sha256-current-regular-symlink-path-mode-content-v2"
SCHEMA_VERSION = 1
GRAPHIFY_DISTRIBUTION = "graphifyy"
GRAPHIFY_VERSION = "0.9.20"
CORE_PREFIX = "docling_serve/"
EXCLUDED_PREFIXES = ("architecture/", "graphify-out/")
DEPENDENCY_RELATIONS = frozenset(
    {"calls", "depends_on", "imports", "imports_from", "inherits", "references", "uses"}
)


class SnapshotChangedError(RuntimeError):
    """The working tree changed while evidence was captured."""


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True
    ).stdout


def snapshot_paths(repository: Path) -> list[Path]:
    raw = _git(
        repository, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    selected: list[Path] = []
    for value in raw.split(b"\0"):
        if not value:
            continue
        relative = Path(value.decode())
        if any(relative.as_posix().startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        try:
            mode = (repository / relative).lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            selected.append(relative)
    return sorted(selected)


def _entry(repository: Path, relative: Path) -> tuple[int, bytes]:
    path = repository / relative
    try:
        path_stat = path.lstat()
        content = (
            path.readlink().as_posix().encode()
            if stat.S_ISLNK(path_stat.st_mode)
            else path.read_bytes()
        )
    except FileNotFoundError as exc:
        raise SnapshotChangedError(f"snapshot path disappeared: {relative}") from exc
    return (0o755 if path_stat.st_mode & stat.S_IXUSR else 0o644), content


def snapshot_digest(repository: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        mode, content = _entry(repository, relative)
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def source_snapshot(repository: Path) -> dict[str, Any]:
    paths = snapshot_paths(repository)
    return {
        "algorithm": SNAPSHOT_ALGORITHM,
        "digest": snapshot_digest(repository, paths),
        "file_count": len(paths),
        "git_sha_supplemental": _git(repository, "rev-parse", "HEAD").decode().strip(),
    }


def materialize_snapshot(repository: Path, destination: Path) -> dict[str, Any]:
    paths = snapshot_paths(repository)
    before = snapshot_digest(repository, paths)
    for relative in paths:
        source = repository / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        else:
            shutil.copy2(source, target)
    after_paths = snapshot_paths(repository)
    if paths != after_paths or before != snapshot_digest(repository, after_paths):
        raise SnapshotChangedError(
            "source changed while architecture snapshot was copied"
        )
    return {
        "algorithm": SNAPSHOT_ALGORITHM,
        "digest": snapshot_digest(destination, paths),
        "file_count": len(paths),
        "git_sha_supplemental": _git(repository, "rev-parse", "HEAD").decode().strip(),
    }


def installed_graphify_version() -> str:
    try:
        return version(GRAPHIFY_DISTRIBUTION)
    except PackageNotFoundError:
        return "not-installed"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
