"""Build a real ACCDB and prove the production Jackcess fallback can read it."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from docling_serve.access.extract import _jackcess_extract
from docling_serve.execution.subprocesses import run_external

_WRITER = """
import io.github.spannm.jackcess.ColumnBuilder;
import io.github.spannm.jackcess.DataType;
import io.github.spannm.jackcess.Database;
import io.github.spannm.jackcess.DatabaseBuilder;
import io.github.spannm.jackcess.Table;
import io.github.spannm.jackcess.TableBuilder;
import java.io.File;

public final class JackcessFixture {
    public static void main(String[] args) throws Exception {
        try (Database database = DatabaseBuilder.create(
                Database.FileFormat.V2010, new File(args[0]))) {
            Table table = new TableBuilder("Items")
                .addColumn(new ColumnBuilder("id", DataType.LONG))
                .addColumn(new ColumnBuilder("name", DataType.TEXT))
                .toTable(database);
            table.addRow(1, "Widget");
        }
    }
}
"""


def main() -> int:
    classpath = os.getenv("DOCLING_SERVE_JACKCESS_CLASSPATH", "").strip()
    if not classpath:
        raise RuntimeError("DOCLING_SERVE_JACKCESS_CLASSPATH is not configured")
    with tempfile.TemporaryDirectory(prefix="jackcess-smoke-") as directory:
        work = Path(directory)
        source = work / "JackcessFixture.java"
        database = work / "inventory.accdb"
        source.write_text(_WRITER, encoding="utf-8")
        run_external(
            ["javac", "-cp", classpath, "-d", str(work), str(source)],
            check=True,
            timeout=60,
        )
        run_external(
            ["java", "-cp", f"{classpath}:{work}", "JackcessFixture", str(database)],
            check=True,
            timeout=60,
        )
        markdown, summaries, tabular = _jackcess_extract(database)
    if "| 1 | Widget |" not in markdown:
        raise RuntimeError("Jackcess smoke row was not extracted")
    if summaries != [{"name": "Items", "columns": 2, "rows": 1}]:
        raise RuntimeError("Jackcess smoke summary is invalid")
    if tabular[0]["rows"] != [{"id": "1", "name": "Widget"}]:
        raise RuntimeError("Jackcess smoke tabular contract is invalid")
    print("Jackcess ACCDB runtime check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
