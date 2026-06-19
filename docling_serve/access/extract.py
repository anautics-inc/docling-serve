"""Microsoft Access (.mdb / .accdb) extraction via the pure-Python access-parser.

docling has no Access backend, so this is a genuine gap. Rather than re-implement
a document model, we convert the database into **docling-native GitHub-flavored
markdown** (one section + table per Access table) — which docling, chunking, and
graph extraction all consume out of the box.

Reading uses `access-parser <https://pypi.org/project/access-parser/>`_, a
pure-Python parser of the Jet/ACE on-disk format. No ODBC, no native library, no
mdbtools CLI — so the dependency is locked in ``uv.lock`` and baked into the
sealed air-gapped image like any other wheel (no OS package, no EPEL).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

ACCESS_SUFFIXES = {".accdb", ".mdb"}

#: Access system/catalog tables (definitions, ACLs, relationships) carry no user
#: data — skip them so the rendered document is just the real tables.
_SYSTEM_TABLE_PREFIXES = ("MSys", "~", "f_", "USysApplicationLog")


class AccessToolsUnavailableError(RuntimeError):
    """Raised when the access-parser library cannot be imported."""


def is_access_file(name: str) -> bool:
    return Path(name).suffix.lower() in ACCESS_SUFFIXES


def _load(path: Path):
    """Open the database with access-parser, mapping import failure to our error."""
    try:
        from access_parser import AccessParser
    except ImportError as err:  # pragma: no cover - dependency is locked in
        raise AccessToolsUnavailableError(
            "access-parser is not installed (pip install access-parser)."
        ) from err
    return AccessParser(str(path))


def _user_tables(db: Any) -> list[str]:
    """User (non-system) table names from the catalog, in catalog order."""
    return [
        name
        for name in (db.catalog or {})
        if not str(name).startswith(_SYSTEM_TABLE_PREFIXES)
    ]


def _table_grid(db: Any, table: str) -> tuple[list[str], list[list[str]]]:
    """Return ``(header, rows)`` for one table.

    access-parser yields a column-oriented ``{column: [values]}`` mapping; we
    transpose it into row-oriented records, padding short columns so a ragged
    table still renders a rectangular grid.
    """
    parsed = db.parse_table(table) or {}
    header = list(parsed.keys())
    if not header:
        return [], []
    row_count = max((len(values) for values in parsed.values()), default=0)
    rows: list[list[str]] = []
    for index in range(row_count):
        rows.append(
            [
                _cell(parsed[col][index]) if index < len(parsed[col]) else ""
                for col in header
            ]
        )
    return header, rows


def access_to_markdown(path: Path) -> tuple[str, list[dict[str, int | str]]]:
    """Render an Access database as docling-native markdown.

    Returns ``(markdown, table_summaries)`` where each summary is
    ``{name, columns, rows}``. Each Access table becomes a ``##`` section with a
    GitHub-flavored markdown table — the same shape docling emits for native
    spreadsheets, so downstream chunking/graph treat it identically.
    """
    db = _load(path)
    parts: list[str] = [f"# {path.stem}", ""]
    summaries: list[dict[str, int | str]] = []
    for table in _user_tables(db):
        try:
            header, rows = _table_grid(db, table)
        except Exception as err:  # one unreadable table must not sink the rest
            _log.warning("access-parser failed on table %s: %s", table, err)
            continue
        summaries.append({"name": table, "columns": len(header), "rows": len(rows)})
        parts.append(f"## {table}")
        parts.append("")
        parts.append(_markdown_table(header, rows))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n", summaries


def dump_schema(path: Path) -> str:
    """A textual schema (table -> column list), best-effort.

    access-parser exposes column names per table rather than typed DDL, so this
    is a lightweight outline — enough for the response/UI to show structure
    without a second parse path.
    """
    try:
        db = _load(path)
        lines: list[str] = []
        for table in _user_tables(db):
            try:
                columns = list((db.parse_table(table) or {}).keys())
            except Exception:  # pragma: no cover - best effort
                columns = []
            lines.append(f"{table} ({', '.join(columns)})")
        return "\n".join(lines)
    except Exception as err:  # pragma: no cover - best effort
        _log.warning("access schema dump failed for %s: %s", path, err)
        return ""


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


def _cell(value: object) -> str:
    return _escape(value)


def _escape(cell: object) -> str:
    # Escape pipes/newlines so a cell value can't break the markdown table grid.
    if cell is None:
        return ""
    return str(cell).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()
