"""Policy boundary for first-party external executables."""

from __future__ import annotations

import os
import resource
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

ALLOWED_EXECUTABLES = frozenset(
    {
        "java",
        "javac",
        "kicad-cli",
        "libreoffice",
        "pdftocairo",
        "pdftoppm",
        "pdftotext",
        "soffice",
        "tesseract",
    }
)
MAX_TIMEOUT_SECONDS = 1800


class ExternalCommandError(RuntimeError):
    """Safe, service-owned failure for an external command."""


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def run_external(
    command: Sequence[str | os.PathLike[str]],
    *,
    timeout: int,
    check: bool = False,
    text: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    if not command:
        raise ValueError("external command is empty")
    executable = Path(os.fspath(command[0])).name
    if executable not in ALLOWED_EXECUTABLES:
        raise ValueError(f"external executable is not allowed: {executable}")
    if timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError("external command timeout is outside service policy")
    runtime_env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    if env:
        runtime_env.update(env)
    try:
        return subprocess.run(
            [os.fspath(part) for part in command],
            capture_output=True,
            check=check,
            text=text,
            timeout=timeout,
            shell=False,
            env=runtime_env,
            preexec_fn=_limits,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise ExternalCommandError(f"{executable} execution failed") from exc
