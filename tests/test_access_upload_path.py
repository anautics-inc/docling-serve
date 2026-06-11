"""Unit tests for the non-docling extractor upload path (issue A2).

The chunk pipeline and bundle assembly must accept results from registry
extractors that bypass docling (AccessExtractor via mdbtools, and any future
extractor) instead of gating on ``ConversionStatus.SUCCESS``. mdbtools is
mocked at the subprocess boundary, matching ``test_extraction_pipeline.py``.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePath
from types import SimpleNamespace

import pytest

from docling.datamodel.document import ConversionStatus
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.targets import InBodyTarget
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.task import Task

import docling_serve.extractors as extractors_pkg
from docling_serve.deep_document.export_results import assemble_bundles
from docling_serve.extraction.chunk_results import (
    process_chunk_results_with_extractors,
)
from docling_serve.extractors import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
    access_extractor as access_mod,
)


def _failed_conv_res(filename: str) -> SimpleNamespace:
    """A docling ConversionResult stand-in for a source docling cannot read."""
    return SimpleNamespace(
        status=ConversionStatus.SKIPPED,
        document=None,
        errors=[],
        timings={},
        input=SimpleNamespace(file=PurePath(filename), document_hash=""),
    )


def _chunk_task(tmp_path: Path, *, metadata: dict | None = None) -> Task:
    return Task(
        task_id="test-task",
        task_type=TaskType.CHUNK,
        sources=[],
        target=InBodyTarget(),
        convert_options=ConvertDocumentsOptions(),
        metadata=metadata or {},
    )


def _mock_mdbtools(monkeypatch):
    monkeypatch.setattr(access_mod, "mdbtools_available", lambda: True)
    monkeypatch.setattr(access_mod, "list_tables", lambda p: ["Parts", "Orders"])
    monkeypatch.setattr(
        access_mod,
        "export_table",
        lambda p, t: (["id", "name"], [["1", f"{t.lower()}-row"]]),
    )
    monkeypatch.setattr(access_mod, "dump_schema", lambda p: "CREATE TABLE Parts (...);")


# --------------------------------------------------------------------------- #
# Chunk pipeline: gate is "an extractor produced units"                        #
# --------------------------------------------------------------------------- #


def test_chunk_pipeline_recovers_access_db(tmp_path, monkeypatch):
    _mock_mdbtools(monkeypatch)
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "inventory.mdb").write_bytes(b"fake-access")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    task = _chunk_task(tmp_path, metadata={"deep_source_dir": str(source_dir)})
    result = process_chunk_results_with_extractors(
        task=task,
        conv_results=[_failed_conv_res("inventory.mdb")],
        work_dir=work_dir,
    )

    # One chunk per table, with table-name + column-header context.
    chunks = result.result.chunks
    assert [c.headings for c in chunks] == [["Parts"], ["Orders"]]
    parts = chunks[0]
    assert parts.filename == "inventory.mdb"
    assert "Parts" in parts.text
    assert "id\tname" in parts.text  # schema/column header context
    assert "parts-row" in parts.text
    assert parts.metadata["domain"] == "database"
    assert parts.metadata["tableName"] == "Parts"
    assert parts.metadata["extractor"] == "extract_access"

    # The document counts as succeeded, not failed.
    assert result.num_succeeded == 1
    assert result.num_failed == 0
    documents = result.result.documents
    assert [d.content.filename for d in documents] == ["inventory.mdb"]
    assert documents[0].status == ConversionStatus.SUCCESS

    # The scratch source dir is cleaned up after processing.
    assert not source_dir.exists()


# A failed .pptx stays failed: PptxExtractor needs the docling conversion, so
# it must never "recover" a docling failure (no behavior change for modern
# formats).
@pytest.mark.parametrize("filename", ["broken.pdf", "broken.pptx"])
def test_chunk_pipeline_keeps_unowned_failures(tmp_path, filename):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = process_chunk_results_with_extractors(
        task=_chunk_task(tmp_path),
        conv_results=[_failed_conv_res(filename)],
        work_dir=work_dir,
    )
    assert result.result.chunks == []
    assert result.num_succeeded == 0
    assert result.num_failed == 1


def test_chunk_pipeline_propagates_typed_extractor_error(tmp_path, monkeypatch):
    # mdbtools missing: the job fails with the extractor's typed error, not a
    # generic docling conversion failure.
    monkeypatch.setattr(access_mod, "mdbtools_available", lambda: False)
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "inventory.mdb").write_bytes(b"fake-access")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    task = _chunk_task(tmp_path, metadata={"deep_source_dir": str(source_dir)})
    with pytest.raises(access_mod.AccessToolsUnavailableError):
        process_chunk_results_with_extractors(
            task=task,
            conv_results=[_failed_conv_res("inventory.mdb")],
            work_dir=work_dir,
        )


class _StubExtractor(Extractor):
    """A future non-docling extractor: owns ``.stub`` files, produces units."""

    name = "extract_stub"
    requires_docling = False

    def supports(self, ctx: ExtractionContext) -> bool:
        return ctx.source_path.suffix.lower() == ".stub"

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        return ExtractorResult(
            structured={
                "document": {
                    "units": [
                        {
                            "unitId": "unit-0001",
                            "unitType": "section",
                            "title": "Stub Section",
                            "content": {"plainText": "stub unit text"},
                        }
                    ]
                }
            },
            extractor=self.name,
            domain="stub-domain",
        )


def test_chunk_pipeline_gate_accepts_any_registry_extractor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        extractors_pkg, "_REGISTRY", [*extractors_pkg._REGISTRY, _StubExtractor()]
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    result = process_chunk_results_with_extractors(
        task=_chunk_task(tmp_path),
        conv_results=[_failed_conv_res("notes.stub")],
        work_dir=work_dir,
    )

    chunks = result.result.chunks
    assert len(chunks) == 1
    assert chunks[0].text == "Stub Section\nstub unit text"
    assert chunks[0].metadata["domain"] == "stub-domain"
    assert result.num_succeeded == 1
    assert result.num_failed == 0


def test_chunk_pipeline_restores_legacy_filenames(tmp_path, monkeypatch):
    monkeypatch.setattr(
        extractors_pkg, "_REGISTRY", [*extractors_pkg._REGISTRY, _StubExtractor()]
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    task = _chunk_task(
        tmp_path,
        metadata={"legacy_office_sources": {"notes.stub": "notes.legacy"}},
    )
    result = process_chunk_results_with_extractors(
        task=task,
        conv_results=[_failed_conv_res("notes.stub")],
        work_dir=work_dir,
    )

    assert result.result.chunks[0].filename == "notes.legacy"
    assert result.result.documents[0].content.filename == "notes.legacy"


# --------------------------------------------------------------------------- #
# Bundle assembly: non-docling extractor results assemble                      #
# --------------------------------------------------------------------------- #


def test_assemble_bundles_includes_access_with_database_domain(tmp_path, monkeypatch):
    _mock_mdbtools(monkeypatch)
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "inventory.mdb").write_bytes(b"fake-access")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    manifests = assemble_bundles(
        conv_results=[_failed_conv_res("inventory.mdb")],
        raw_dir=raw_dir,
        output_dir=output_dir,
        task_id="test-task",
        source_dir=source_dir,
    )

    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest["extractor"] == "extract_access"
    assert manifest["domain"] == "database"
    assert manifest["source"]["originalFileName"] == "inventory.mdb"
    assert manifest["counts"]["units"] == 2  # one unit per table

    # Single-document upload: bundle at the output root with all sidecars.
    written = json.loads((output_dir / "extraction.json").read_text())
    assert written["domain"] == "database"
    assert (output_dir / "document.json").is_file()
    assert (output_dir / "access-tables.json").is_file()
    assert (output_dir / "access-schema.sql").is_file()
    assert (output_dir / "tables" / "Parts.csv").is_file()
    assert (output_dir / "tables" / "Orders.csv").is_file()
    inventory = json.loads((output_dir / "access-tables.json").read_text())
    assert [t["table"] for t in inventory["tables"]] == ["Parts", "Orders"]


@pytest.mark.parametrize("filename", ["broken.pdf", "broken.pptx"])
def test_assemble_bundles_still_skips_unowned_failures(tmp_path, filename):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    manifests = assemble_bundles(
        conv_results=[_failed_conv_res(filename)],
        raw_dir=raw_dir,
        output_dir=output_dir,
        task_id="test-task",
    )
    assert manifests == []
    assert list(output_dir.iterdir()) == []
