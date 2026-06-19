"""Microsoft Access (.mdb / .accdb) extraction via mdbtools.

docling has no Access backend, so this is a genuine gap. Rather than re-implement
a document model, we convert the database into **docling-native GitHub-flavored
markdown** (one section + table per Access table) — which docling, chunking, and
graph extraction all consume out of the box. Reading uses the mdbtools CLI
(``mdb-tables`` / ``mdb-export`` / ``mdb-schema``) — pure Linux, no ODBC.
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

ACCESS_SUFFIXES = {".accdb", ".mdb"}
_SUBPROCESS_TIMEOUT = 600


class AccessToolsUnavailableError(RuntimeError):
    """Raised when the mdbtools CLI is not installed."""


def is_access_file(name: str) -> bool:
    return Path(name).suffix.lower() in ACCESS_SUFFIXES


def mdbtools_available() -> bool:
    return shutil.which("mdb-export") is not None and shutil.which("mdb-tables") is not None


def list_tables(path: Path) -> list[str]:
    # ``--`` terminates option parsing so a path/table beginning with ``-`` cannot be
    # reinterpreted as a flag (argument injection).
    output = _run(["mdb-tables", "-1", "--", str(path)])
    return [line.strip() for line in output.splitlines() if line.strip()]


def export_table(path: Path, table: str) -> tuple[list[str], list[list[str]]]:
    """Return ``(header, rows)`` for one table via ``mdb-export``.

    ``table`` originates from inside the (untrusted) database file, so ``--`` guards
    against a table name beginning with ``-`` being parsed as a flag.
    """
    output = _run(["mdb-export", "--", str(path), table])
    records = list(csv.reader(io.StringIO(output)))
    if not records:
        return [], []
    return records[0], records[1:]


def dump_schema(path: Path) -> str:
    try:
        return _run(["mdb-schema", str(path)])
    except Exception as err:  # pragma: no cover - best effort
        _log.warning("mdb-schema failed for %s: %s", path, err)
        return ""


def access_to_markdown(path: Path) -> tuple[str, list[dict[str, int | str]]]:
    """Render an Access database as docling-native markdown.

    Returns ``(markdown, table_summaries)`` where each summary is
    ``{name, columns, rows}``. Each Access table becomes a ``##`` section with a
    GitHub-flavored markdown table — the same shape docling emits for native
    spreadsheets, so downstream chunking/graph treat it identically.
    """
    if not mdbtools_available():
        raise AccessToolsUnavailableError(
            "mdbtools is not installed (need mdb-tables / mdb-export)."
        )
    tables = list_tables(path)
    parts: list[str] = [f"# {path.stem}", ""]
    summaries: list[dict[str, int | str]] = []
    for table in tables:
        header, rows = export_table(path, table)
        summaries.append({"name": table, "columns": len(header), "rows": len(rows)})
        parts.append(f"## {table}")
        parts.append("")
        parts.append(_markdown_table(header, rows))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n", summaries


def _markdown_table(header: list[str], rows: list[list[str]]) -> str:
    if not header:
        return "_(empty table)_"
    cols = len(header)

    def _row(cells: list[str]) -> str:
        padded = list(cells) + [""] * (cols - len(cells))
        return "| " + " | ".join(_escape(c) for c in padded[:cols]) + " |"

    lines = [_row(header), "| " + " | ".join(["---"] * cols) + " |"]
    lines.extend(_row(row) for row in rows)
    return "\n".join(lines)


def _escape(cell: object) -> str:
    # Escape pipes/newlines so a cell value can't break the markdown table grid.
    return str(cell).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
        )
    except FileNotFoundError as err:
        raise AccessToolsUnavailableError(str(err)) from err
    except subprocess.CalledProcessError as err:
        raise RuntimeError(f"{args[0]} failed: {err.stderr.strip() or err}") from err
    return result.stdout
