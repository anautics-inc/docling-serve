"""Microsoft Access (.mdb/.accdb) extractor via mdbtools.

Docling cannot read an Access database, so this extractor bypasses the Docling
conversion result entirely and reads the file directly with the ``mdbtools``
CLI (``mdb-tables`` / ``mdb-export`` / ``mdb-schema``) — pure Linux, no ODBC
driver required. Each table becomes a unit in the deep document, full rows are
written as CSV sidecars, and a table/column inventory is emitted for downstream
NER / graph building.

Requires the ``mdbtools`` OS package (see ``os-packages.txt``). When it is
missing the extractor raises :class:`AccessToolsUnavailableError`.
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from docling_serve.deep_document.artifact_writer import write_json
from docling_serve.deep_document.document_builder import build_deep_document
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)

_log = logging.getLogger(__name__)

ACCESS_SUFFIXES = {".accdb", ".mdb"}
ACCESS_PROFILES = {"access", "accessdb", "database"}
# Rows materialised into JSON / unit text per table (full data still lands in CSV).
MAX_PREVIEW_ROWS = 200


class AccessToolsUnavailableError(RuntimeError):
    """Raised when the mdbtools CLI is not installed."""


def mdbtools_available() -> bool:
    return shutil.which("mdb-export") is not None and shutil.which("mdb-tables") is not None


class AccessExtractor(Extractor):
    name = "extract_access"

    def supports(self, ctx: ExtractionContext) -> bool:
        if ctx.source_path.suffix.lower() in ACCESS_SUFFIXES:
            return True
        return (ctx.profile or "").strip().lower() in ACCESS_PROFILES

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        if not mdbtools_available():
            raise AccessToolsUnavailableError(
                "mdbtools is not installed (need mdb-tables/mdb-export). "
                "Add the 'mdbtools' OS package."
            )
        source_file = ctx.resolve_source_file()
        if not source_file.is_file():
            raise FileNotFoundError(f"Access database not found: {source_file}")

        tables = list_tables(source_file)
        tables_dir = ctx.bundle_dir / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)

        units: list[dict[str, Any]] = []
        inventory: list[dict[str, Any]] = []
        artifacts: list[str] = []
        total_rows = 0

        for index, table in enumerate(tables):
            header, rows = export_table(source_file, table)
            total_rows += len(rows)

            csv_path = tables_dir / f"{_safe_name(table)}.csv"
            _write_csv(csv_path, header, rows)
            artifacts.append(csv_path.relative_to(ctx.bundle_dir).as_posix())

            preview = rows[:MAX_PREVIEW_ROWS]
            units.append(
                {
                    "unitId": f"unit-{index + 1:04d}",
                    "unitType": "table",
                    "index": index,
                    "title": table,
                    "sourceRefs": {"tableName": table},
                    "elements": [
                        {
                            "elementId": f"unit-{index + 1:04d}-element-0001",
                            "type": "table",
                            "kind": "table",
                            "text": {
                                "plain": _table_text(header, preview),
                                "paragraphs": [],
                                "runs": [],
                            },
                            "table": {
                                "columns": header,
                                "rowCount": len(rows),
                                "cells": _cells(header, preview),
                            },
                        }
                    ],
                }
            )
            inventory.append(
                {"table": table, "columns": header, "rowCount": len(rows)}
            )

        manifest = {
            "artifactKind": "captify.access.normalizedExtraction.v1",
            "source": {
                "originalFileName": ctx.source_path.name,
                "fileKind": "database",
                "sourceManifestKey": ctx.source_manifest_key,
            },
            "units": units,
            "assets": [],
        }
        structured = build_deep_document(
            manifest=manifest, source_manifest_key=ctx.source_manifest_key
        )
        structured["database"] = {
            "engine": "msaccess",
            "tableCount": len(tables),
            "rowCount": total_rows,
            "tables": [item["table"] for item in inventory],
        }
        validate_artifact(structured, "deep-document.schema.json")

        inventory_path = ctx.bundle_dir / "access-tables.json"
        write_json(inventory_path, {"tables": inventory})
        artifacts.append(inventory_path.relative_to(ctx.bundle_dir).as_posix())

        schema_text = dump_schema(source_file)
        if schema_text:
            schema_path = ctx.bundle_dir / "access-schema.sql"
            schema_path.write_text(schema_text)
            artifacts.append(schema_path.relative_to(ctx.bundle_dir).as_posix())

        return ExtractorResult(
            structured=structured,
            extractor=self.name,
            domain="database",
            artifacts=artifacts,
            manifest_extra={
                "database": {
                    "engine": "msaccess",
                    "tableCount": len(tables),
                    "rowCount": total_rows,
                    "inventory": inventory_path.relative_to(ctx.bundle_dir).as_posix(),
                }
            },
        )


def list_tables(path: Path) -> list[str]:
    # ``--`` terminates option parsing so a path/table that begins with ``-``
    # cannot be reinterpreted as a flag (argument injection).
    output = _run(["mdb-tables", "-1", "--", str(path)])
    return [line.strip() for line in output.splitlines() if line.strip()]


def export_table(path: Path, table: str) -> tuple[list[str], list[list[str]]]:
    """Return ``(header, rows)`` for one table via ``mdb-export``.

    ``table`` originates from inside the (untrusted) database file, so ``--``
    guards against a table name that begins with ``-`` being parsed as a flag.
    """
    output = _run(["mdb-export", "--", str(path), table])
    reader = csv.reader(io.StringIO(output))
    records = list(reader)
    if not records:
        return [], []
    return records[0], records[1:]


def dump_schema(path: Path) -> str:
    try:
        return _run(["mdb-schema", str(path)])
    except Exception as err:  # pragma: no cover - best effort
        _log.warning("mdb-schema failed for %s: %s", path, err)
        return ""


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=600
        )
    except FileNotFoundError as err:
        raise AccessToolsUnavailableError(str(err)) from err
    except subprocess.CalledProcessError as err:
        raise RuntimeError(
            f"{args[0]} failed: {err.stderr.strip() or err}"
        ) from err
    return result.stdout


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if header:
            writer.writerow(header)
        writer.writerows(rows)


def _table_text(header: list[str], rows: list[list[str]]) -> str:
    lines = []
    if header:
        lines.append("\t".join(header))
    for row in rows:
        lines.append("\t".join(str(cell) for cell in row))
    return "\n".join(lines)


def _cells(header: list[str], rows: list[list[str]]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for col_index, name in enumerate(header):
        cells.append({"text": name, "rowIndex": 0, "colIndex": col_index})
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row):
            cells.append({"text": value, "rowIndex": row_index, "colIndex": col_index})
    return cells


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name) or "table"
