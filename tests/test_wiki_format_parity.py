"""Hermetic regression tests pinning the public extraction routing/contracts the
Wiki (Spaces ingestion) relies on, per source format family.

Wiki submits documents to docling-serve and depends on:

- generic documents / images / PPTX decks / XLSX+CSV spreadsheets going through
  ``/v1/convert/file/async`` (docling's own format registry does the routing),
- Access databases going through ``/v1/extract/access`` (access-parser, no
  mdbtools/ODBC),
- XFA / AF dynamic forms going through ``/v1/extract/form`` (pikepdf, the
  ``captify.form.v1`` payload),
- engineering drawings going through ``/v1/extract/schematic``
  (``captify.schematic.v1``),
- technical orders (IPB/RPSTL BOM + embedded figures) going through
  ``/v1/extract/technical-order`` (``captify.bom.v1``).

Everything here is hermetic: no Bedrock/LiteLLM, no AWS, no LibreOffice, no
mdbtools, no network. Heavy pipelines are pinned at the route contract with
lightweight fakes (same pattern as ``test_batch_endpoint.py``); pure-Python
extractors (XFA, Access markdown rendering) run for real against synthesized
inputs. Formats whose real parse inherently needs environment tooling carry a
precise ``skip`` plus a separate route/registry support check.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pikepdf
import pytest
from httpx import ASGITransport, AsyncClient

from docling.datamodel.base_models import (
    FormatToExtensions,
    InputFormat,
)
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.sources import FileSource
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.task import Task

from docling_serve.access import ACCESS_SUFFIXES, is_access_file
from docling_serve.form import XFA_PROFILES, extract_xfa_form, is_xfa_pdf
from docling_serve.schematic.extract import is_schematic_candidate
from docling_serve.schematic.schematic_extractor import (
    SCHEMATIC_PROFILES,
    SCHEMATIC_SUFFIXES,
)
from docling_serve.technical_order.bundle import BOM_SCHEMA_ID
from docling_serve.technical_order.extract import TO_PROFILES
from docling_serve.upload_staging import (
    STAGED_PLACEHOLDER_PREFIX,
    STAGED_UPLOAD_METADATA_KEY,
    StagedUpload,
    StagedUploadRef,
    UploadStagingDisabled,
    UploadStagingInputError,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """Records enqueue calls; never touches Redis/Ray/workers."""

    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return Task(
            task_id="task-wiki",
            task_type=kwargs["task_type"],
            sources=kwargs["sources"],
            target=kwargs["target"],
            convert_options=kwargs["convert_options"],
            callbacks=kwargs["callbacks"],
            metadata=kwargs["metadata"],
        )

    async def get_queue_position(self, task_id: str):
        del task_id
        return 0


class _FakeUploadStager:
    def __init__(self):
        self.staged: list[dict[str, object]] = []
        self.cleaned: list[dict[str, str]] = []

    async def stage(self, *, payload, filename, content_type, tenant_id):
        record = {
            "payload": payload,
            "filename": filename,
            "content_type": content_type,
            "tenant_id": tenant_id,
        }
        self.staged.append(record)
        upload_id = f"{len(self.staged):032x}"
        ref = StagedUploadRef(
            upload_id=upload_id,
            bucket_id="b" * 64,
            key=f"docling-staging/v1/{'a' * 64}/{upload_id}",
            checksum_sha256="c" * 64,
            size_bytes=len(payload),
            content_type=content_type,
            original_name=filename,
            tenant_hash="a" * 64,
        )
        return StagedUpload(
            source=FileSource(
                filename=f"{STAGED_PLACEHOLDER_PREFIX}{upload_id}",
                base64_string="",
            ),
            ref=ref,
        )

    async def cleanup(self, metadata):
        self.cleaned.extend(metadata)


@pytest.fixture
def fake_orchestrator(monkeypatch):
    from docling_serve import app as app_module

    orchestrator = _FakeOrchestrator()
    stager = _FakeUploadStager()
    monkeypatch.setattr(app_module.docling_serve_settings, "auth_mode", "none")
    monkeypatch.setattr(app_module.docling_serve_settings, "api_key", "")
    monkeypatch.setattr(app_module.docling_serve_settings, "allow_no_auth", True)
    # Keep every route on its inline (no-S3, no-vision) path.
    monkeypatch.setattr(
        app_module.docling_serve_settings, "artifact_storage_bucket", ""
    )
    monkeypatch.setattr(
        app_module.docling_serve_settings, "figure_hotspot_vision", False
    )
    monkeypatch.setattr(app_module.docling_serve_settings, "litellm_base_url", None)
    monkeypatch.setattr(app_module.docling_serve_settings, "litellm_api_key", None)
    monkeypatch.setattr(app_module, "get_async_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(app_module, "build_upload_stager", lambda: stager)
    orchestrator.stager = stager
    return orchestrator


@pytest.fixture
def app(fake_orchestrator):
    from docling_serve import app as app_module

    del fake_orchestrator
    with patch.object(app_module, "setup_otel_instrumentation"):
        return app_module.create_app()


def _client(app) -> AsyncClient:
    # ASGITransport does not run the lifespan, so no queue processor starts.
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://app.io")


def _build_plain_pdf() -> bytes:
    buf = io.BytesIO()
    pikepdf.new().save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_disabled_staging_makes_file_upload_endpoint_unavailable(
    app, monkeypatch
):
    from docling_serve import app as app_module

    def disabled():
        raise UploadStagingDisabled("disabled")

    monkeypatch.setattr(app_module, "build_upload_stager", disabled)
    async with _client(app) as client:
        response = await client.post(
            "/v1/convert/file/async",
            files={"files": ("report.doc", b"bytes", "application/msword")},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "File uploads are disabled on this deployment."


@pytest.mark.asyncio
async def test_hostile_upload_mime_is_a_typed_client_rejection(app, monkeypatch):
    from docling_serve import app as app_module

    class RejectingStager:
        async def stage(self, **kwargs):
            del kwargs
            raise UploadStagingInputError("invalid")

        async def cleanup(self, refs):
            assert refs == []

    monkeypatch.setattr(app_module, "build_upload_stager", RejectingStager)
    async with _client(app) as client:
        response = await client.post(
            "/v1/convert/file/async",
            files={
                "files": (
                    "report.doc",
                    b"bytes",
                    "text/plain; X-Amz-Signature=secret",
                )
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "Upload filename or media type is invalid."


_XFA_TEMPLATE = b"""<template xmlns="http://www.xfa.org/schema/xfa-template/3.3/">
<subform name="form1"><pageSet><pageArea name="Page1"/></pageSet>
<subform name="Page1"><subform name="SectionA" x="0mm" y="0mm">
<field name="OrgName" x="10mm" y="20mm" w="80mm" h="8mm">
<ui><textEdit/></ui><caption><value><text>Organization</text></value></caption>
</field>
<field name="POC" x="10mm" y="30mm" w="80mm" h="8mm">
<ui><textEdit/></ui><caption><value><text>Point of Contact</text></value></caption>
</field>
<draw name="Title" x="10mm" y="5mm" w="80mm" h="8mm">
<value><text>Market Research Report</text></value>
</draw>
</subform></subform></subform></template>"""

_XFA_DATASETS = b"""<xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/">
<xfa:data><form1><Page1><SectionA><OrgName>AFMC</OrgName><POC/></SectionA></Page1>
</form1></xfa:data></xfa:datasets>"""


def _build_xfa_pdf() -> bytes:
    """A minimal LiveCycle-style dynamic PDF, synthesized entirely in-process."""
    pdf = pikepdf.new()
    xfa = pikepdf.Array(
        [
            pikepdf.String("template"),
            pdf.make_stream(_XFA_TEMPLATE),
            pikepdf.String("datasets"),
            pdf.make_stream(_XFA_DATASETS),
        ]
    )
    pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(XFA=xfa))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


class _FakeAccessDb:
    """access-parser stand-in: column-oriented tables + a system-table entry."""

    catalog = {"Parts": object(), "MSysObjects": object()}

    def parse_table(self, table: str):
        assert table == "Parts", "system tables must never be parsed"
        return {
            "PartNo": ["P|100", "P-200"],
            "Description": ["Bolt\nhex", "Nut"],
        }


@pytest.fixture
def fake_access_db(monkeypatch):
    from docling_serve.access import extract as access_extract

    db = _FakeAccessDb()
    monkeypatch.setattr(access_extract, "_load", lambda path: db)
    return db


# ---------------------------------------------------------------------------
# Registry / routing support (unit layer — no app, no I/O)
# ---------------------------------------------------------------------------


def test_generic_document_formats_are_in_docling_registry():
    assert "pdf" in FormatToExtensions[InputFormat.PDF]
    assert "docx" in FormatToExtensions[InputFormat.DOCX]
    assert {"html", "htm"} <= set(FormatToExtensions[InputFormat.HTML])
    assert {"md", "txt"} <= set(FormatToExtensions[InputFormat.MD])
    assert {"adoc", "asciidoc"} <= set(FormatToExtensions[InputFormat.ASCIIDOC])


def test_image_formats_are_in_docling_registry():
    assert {"jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp"} <= set(
        FormatToExtensions[InputFormat.IMAGE]
    )


def test_deck_and_spreadsheet_formats_are_in_docling_registry():
    assert {"pptx", "ppsx", "pptm"} <= set(FormatToExtensions[InputFormat.PPTX])
    assert {"xlsx", "xlsm"} <= set(FormatToExtensions[InputFormat.XLSX])
    assert "csv" in FormatToExtensions[InputFormat.CSV]


def test_default_convert_options_enable_every_wiki_convert_format():
    """Wiki posts without ``from_formats``; the server default must keep every
    convert-path family enabled."""
    enabled = {f.value for f in ConvertDocumentsOptions().from_formats}
    assert {"pdf", "docx", "html", "md", "image", "pptx", "xlsx", "csv"} <= enabled


def test_legacy_binary_office_formats_have_native_and_isolated_fallback_support():
    """Current Docling accepts binary Office formats, while the isolated
    LibreOffice adapter remains available for deployments that require fallback
    normalization of problematic historical files."""
    every_extension = {ext for exts in FormatToExtensions.values() for ext in exts}
    assert {"ppt", "xls", "doc"} <= every_extension
    for source_format in ("ppt", "xls", "doc"):
        assert source_format in {
            value.value
            for value in ConvertDocumentsOptions(
                from_formats=[source_format]
            ).from_formats
        }


def test_access_suffix_routing():
    assert ACCESS_SUFFIXES == {".mdb", ".accdb"}
    assert is_access_file("inventory.mdb")
    assert is_access_file("Inventory.ACCDB")
    assert not is_access_file("inventory.xlsx")
    assert not is_access_file("inventory.pdf")


def test_schematic_suffix_and_profile_routing():
    assert {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".svg"} <= (
        SCHEMATIC_SUFFIXES
    )
    assert {"schematic", "drawing", "technical-order-schematic"} <= (SCHEMATIC_PROFILES)
    assert is_schematic_candidate(Path("wiring.pdf"), profile="schematic")
    assert is_schematic_candidate(Path("board.png"), profile="drawing")
    assert not is_schematic_candidate(Path("notes.docx"), profile="schematic")


def test_form_and_technical_order_profile_registries():
    assert {"xfa", "form", "af-form", "dod-form"} <= XFA_PROFILES
    assert {"technical-order", "to", "ipb"} <= TO_PROFILES
    assert BOM_SCHEMA_ID == "captify.bom.v2"


async def test_openapi_exposes_every_wiki_extraction_route(app):
    async with _client(app) as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for route in (
        "/v1/convert/file",
        "/v1/convert/file/async",
        "/v1/extract/access",
        "/v1/extract/auto",
        "/v1/extract/form",
        "/v1/extract/technical-order",
        "/v1/extract/schematic",
        "/v1/capabilities",
        "/v1/status/poll/{task_id}",
        "/v1/result/{task_id}",
    ):
        assert route in paths, f"Wiki-facing route missing from API: {route}"


@pytest.mark.asyncio
async def test_capabilities_endpoint_exposes_contract_and_availability(app):
    async with _client(app) as client:
        response = await client.get("/v1/capabilities")
    assert response.status_code == 200
    capabilities = {item["name"]: item for item in response.json()["capabilities"]}
    assert capabilities["document"]["available"] is True
    assert capabilities["technical-order"]["outputContract"] == "captify.bom.v2"
    assert ".doc" in capabilities["legacy-office"]["extensions"]


@pytest.mark.asyncio
async def test_auto_extract_keeps_generic_document_on_async_pipeline(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/auto",
            files=[("files", ("report.pdf", _build_plain_pdf(), "application/pdf"))],
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "domain": "document",
        "routing": {
            "domain": "document",
            "reason": "generic supported format",
            "ocrPolicy": "auto",
        },
    }


# ---------------------------------------------------------------------------
# Generic documents / images / decks / spreadsheets -> /v1/convert/file/async
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "report.pdf",
        "notes.docx",
        "page.html",
        "readme.md",
        "deck.pptx",
        "book.xlsx",
        "data.csv",
        "scan.png",
        "photo.tiff",
    ],
)
async def test_convert_file_async_enqueues_each_wiki_format(
    app, fake_orchestrator, filename
):
    """The convert route stages a serializable source reference for every format."""
    async with _client(app) as client:
        response = await client.post(
            "/v1/convert/file/async",
            files=[("files", (filename, b"wiki-bytes", "application/octet-stream"))],
            data={"to_formats": "md"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == "task-wiki"
    assert body["task_type"] == TaskType.CONVERT

    enqueued = fake_orchestrator.enqueued[-1]
    assert enqueued["task_type"] == TaskType.CONVERT
    source = enqueued["sources"][0]
    assert isinstance(source, FileSource)
    assert (
        enqueued["metadata"][STAGED_UPLOAD_METADATA_KEY][0]["original_name"] == filename
    )


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("legacy.doc", "application/msword"),
        ("legacy.ppt", "application/vnd.ms-powerpoint"),
        ("legacy.xls", "application/vnd.ms-excel"),
    ],
)
async def test_convert_file_async_routes_legacy_office_to_durable_worker(
    app, fake_orchestrator, filename, content_type
):
    """Submission remains asynchronous: original names and tenant metadata are
    durable in the task, and worker-side preconversion performs extraction."""
    async with _client(app) as client:
        response = await client.post(
            "/v1/convert/file/async",
            files=[
                (
                    "files",
                    (
                        filename,
                        b"\xd0\xcf\x11\xe0",
                        content_type,
                    ),
                )
            ],
            data={"to_formats": "md"},
            headers={"X-Tenant-Id": "wiki-tenant"},
        )
    assert response.status_code == 200
    enqueued = fake_orchestrator.enqueued[-1]
    assert isinstance(enqueued["sources"][0], FileSource)
    assert enqueued["metadata"]["tenant_id"] == "wiki-tenant"
    assert (
        enqueued["metadata"][STAGED_UPLOAD_METADATA_KEY][0]["original_name"] == filename
    )
    assert (
        enqueued["metadata"][STAGED_UPLOAD_METADATA_KEY][0]["content_type"]
        == content_type
    )


async def test_convert_upload_enforces_actual_bytes_not_declared_size(
    app, fake_orchestrator, monkeypatch
):
    from docling_serve import app as app_module

    monkeypatch.setattr(app_module.docling_serve_settings, "max_file_size", 4)
    async with _client(app) as client:
        response = await client.post(
            "/v1/convert/file/async",
            files=[("files", ("overflow.doc", b"12345", "application/msword"))],
            data={"to_formats": "md"},
        )
    assert response.status_code == 413
    assert fake_orchestrator.enqueued == []


async def test_legacy_upload_uses_lower_format_limit_and_preserves_mime(
    app, fake_orchestrator, monkeypatch
):
    from docling_serve import app as app_module

    monkeypatch.setattr(app_module.docling_serve_settings, "max_file_size", 10)
    monkeypatch.setattr(
        app_module.docling_serve_settings,
        "legacy_office_max_input_bytes",
        4,
    )
    async with _client(app) as client:
        overflow = await client.post(
            "/v1/convert/file/async",
            files=[("files", ("unknown.bin", b"12345", "application/msword"))],
            data={"to_formats": "md"},
        )
        accepted = await client.post(
            "/v1/convert/file/async",
            files=[
                (
                    "files",
                    ("legacy.doc", b"1234", "application/octet-stream"),
                )
            ],
            data={"to_formats": "md"},
        )
        normal = await client.post(
            "/v1/convert/file/async",
            files=[("files", ("normal.pdf", b"12345", "application/pdf"))],
            data={"to_formats": "md"},
        )
    assert overflow.status_code == 413
    assert accepted.status_code == 200
    assert normal.status_code == 200
    legacy_source = fake_orchestrator.enqueued[-2]["sources"][0]
    assert isinstance(legacy_source, FileSource)
    task_metadata = fake_orchestrator.enqueued[-2]["metadata"]
    assert task_metadata[STAGED_UPLOAD_METADATA_KEY][0]["original_name"] == (
        "legacy.doc"
    )
    assert (
        task_metadata[STAGED_UPLOAD_METADATA_KEY][0]["content_type"]
        == "application/octet-stream"
    )


# ---------------------------------------------------------------------------
# Access (.mdb/.accdb) -> /v1/extract/access
# ---------------------------------------------------------------------------


def test_access_markdown_rendering_contract(fake_access_db, tmp_path):
    """The real renderer over a fake Jet catalog: one ``##`` section + GFM table
    per user table, system tables skipped, pipes/newlines escaped."""
    from docling_serve.access.extract import access_to_markdown, dump_schema

    markdown, tables = access_to_markdown(tmp_path / "inventory.mdb")

    assert tables == [{"name": "Parts", "columns": 2, "rows": 2}]
    assert markdown == (
        "# inventory\n"
        "\n"
        "## Parts\n"
        "\n"
        "| PartNo | Description |\n"
        "| --- | --- |\n"
        "| P\\|100 | Bolt hex |\n"
        "| P-200 | Nut |\n"
    )
    assert dump_schema(tmp_path / "inventory.mdb") == "Parts (PartNo, Description)"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("|", "\\|", id="raw-pipe"),
        pytest.param("\\|", "\\|", id="odd-backslash-run"),
        pytest.param("\\\\|", "\\\\\\|", id="even-backslash-run"),
        pytest.param(
            "raw| odd\\| even\\\\| tail\nnext",
            "raw\\| odd\\| even\\\\\\| tail next",
            id="mixed-value",
        ),
    ],
)
def test_access_markdown_pipe_escaping_is_single_pass(
    value: str, expected: str
) -> None:
    """Every rendered pipe has an odd preceding slash run, without slash loss."""
    from docling_serve.access.extract import _escape

    escaped = _escape(value)

    assert escaped == expected
    for pipe_index, char in enumerate(escaped):
        if char != "|":
            continue
        slash_count = 0
        index = pipe_index - 1
        while index >= 0 and escaped[index] == "\\":
            slash_count += 1
            index -= 1
        assert slash_count % 2 == 1


def test_access_markdown_table_preserves_single_pass_cell_escaping():
    from docling_serve.access.extract import _markdown_table

    assert _markdown_table(
        ["Field|Name"],
        [["raw| odd\\| even\\\\| tail\nnext"]],
    ) == ("| Field\\|Name |\n| --- |\n| raw\\| odd\\| even\\\\\\| tail next |")


async def test_extract_access_route_contract(app, fake_access_db):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/access",
            files=[
                ("files", ("inventory.mdb", b"jet-bytes", "application/octet-stream"))
            ],
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "inventory.mdb"
    assert body["tables"] == [{"name": "Parts", "columns": 2, "rows": 2}]
    assert "## Parts" in body["markdown"]
    assert body["schema"] == "Parts (PartNo, Description)"


async def test_extract_access_route_rejects_non_access_upload(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/access",
            files=[("files", ("report.pdf", b"%PDF-1.4", "application/pdf"))],
        )
    assert response.status_code == 422
    assert ".mdb or .accdb" in response.text


@pytest.mark.skip(
    reason=(
        "Parsing a REAL Access database requires a binary Jet/ACE .mdb fixture; "
        "the on-disk format cannot be synthesized hermetically (access-parser is "
        "read-only and no mdbtools/ODBC writer is allowed at this layer). Route "
        "and suffix support are pinned in test_access_suffix_routing / "
        "test_extract_access_route_contract."
    )
)
def test_real_access_database_parse():
    raise AssertionError("unreachable: skipped")


# ---------------------------------------------------------------------------
# XFA / AF dynamic forms -> /v1/extract/form
# ---------------------------------------------------------------------------


def test_xfa_extractor_payload_contract(tmp_path):
    """The real pikepdf+XFA extractor over a synthesized LiveCycle PDF must keep
    the captify.form.v1 shape the Wiki form registrar consumes."""
    src = tmp_path / "af-form.pdf"
    src.write_bytes(_build_xfa_pdf())

    assert is_xfa_pdf(src)
    payload = extract_xfa_form(src, source_key="af-form.pdf")

    assert payload["schema"] == "captify.form.v1"
    assert payload["format"] == "xfa"
    assert payload["fieldCount"] == 2
    assert payload["labelCount"] == 1
    assert payload["boundValueCount"] == 1  # OrgName bound from datasets
    assert payload["sections"] == ["SectionA"]
    assert payload["hasDatasets"] is True
    # Every fillable leaf (including the empty POC) for the registrar.
    assert "form1.Page1.SectionA.OrgName" in payload["datasetLeafPaths"]
    assert "form1.Page1.SectionA.POC" in payload["datasetLeafPaths"]
    org = next(f for f in payload["fields"] if f["name"] == "OrgName")
    assert org["path"] == "form1.Page1.SectionA.OrgName"
    assert org["boundValue"] == "AFMC"
    assert org["bbox"]["unit"] == "mm"
    assert payload["units"][0]["unitType"] == "form_section"
    assert "**Organization:** AFMC" in payload["markdown"]


async def test_extract_form_route_contract(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/form",
            files=[("files", ("af1067.pdf", _build_xfa_pdf(), "application/pdf"))],
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "captify.form.v1"
    assert body["fieldCount"] == 2
    assert body["source"]["filename"] == "af1067.pdf"
    # Inline path: nothing published without a prefix + bucket.
    assert body["s3Keys"] == []
    assert body["bucket"] is None


async def test_auto_form_reuses_form_domain_execution(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/auto",
            data={"profile": "form"},
            files=[("files", ("af1067.pdf", _build_xfa_pdf(), "application/pdf"))],
        )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["schema"] == "captify.form.v1"
    assert body["routing"]["domain"] == "form"


async def test_extract_form_route_rejects_pdf_without_xfa(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/form",
            files=[("files", ("plain.pdf", _build_plain_pdf(), "application/pdf"))],
        )
    assert response.status_code == 422
    assert "no XFA" in response.text


async def test_extract_form_route_rejects_non_pdf(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/form",
            files=[("files", ("form.docx", b"PK", "application/octet-stream"))],
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Schematics -> /v1/extract/schematic
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_schematic_extractor(monkeypatch):
    from docling_serve.schematic import extract as schematic_extract

    calls: list[dict] = []

    def _fake(
        pdf_path, output_dir, *, profile, tenant_id=None, source_key=None, progress=None
    ):
        calls.append(
            {
                "name": Path(pdf_path).name,
                "profile": profile,
                "tenant_id": tenant_id,
                "source_key": source_key,
            }
        )
        return {
            "structured": {},
            "artifacts": ["schematic/schematic-graph.json", "schematic/schematic.svg"],
            "manifest": {},
            "graph": {"schema": "captify.schematic.v1", "components": [], "nets": []},
            "domain": "schematic",
            "notes": ["hermetic-fake"],
        }

    monkeypatch.setattr(schematic_extract, "extract_schematic", _fake)
    return calls


async def test_extract_schematic_route_contract(app, fake_schematic_extractor):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/schematic",
            files=[("files", ("wiring.pdf", b"%PDF-1.4", "application/pdf"))],
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["domain"] == "schematic"
    assert body["graph"]["schema"] == "captify.schematic.v1"
    assert "schematic/schematic-graph.json" in body["artifacts"]
    assert body["notes"] == ["hermetic-fake"]
    assert body["s3Keys"] == []
    call = fake_schematic_extractor[-1]
    assert call["name"] == "wiring.pdf"
    assert call["profile"] == "schematic"  # route default forces the extractor
    assert call["source_key"] == "wiring.pdf"


async def test_auto_schematic_reuses_schematic_domain_execution(
    app, fake_schematic_extractor
):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/auto",
            data={"profile": "schematic"},
            files=[("files", ("wiring.pdf", b"%PDF-1.4", "application/pdf"))],
        )
    assert response.status_code == 200, response.text
    assert response.json()["routing"]["domain"] == "schematic"
    assert fake_schematic_extractor[-1]["profile"] == "schematic"


async def test_extract_schematic_route_accepts_image_upload(
    app, fake_schematic_extractor
):
    """Raster drawings (.png/.jpg/.tiff) are first-class schematic inputs."""
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/schematic",
            files=[("files", ("board.png", b"\x89PNG", "image/png"))],
        )
    assert response.status_code == 200
    assert fake_schematic_extractor[-1]["name"] == "board.png"


@pytest.mark.skip(
    reason=(
        "Real schematic extraction requires the Bedrock vision model (LiteLLM "
        "proxy, network) plus the rendering/OCR toolchain — inherently "
        "environment tooling forbidden at this layer. Route contract and "
        "suffix/profile routing are pinned in test_extract_schematic_route_* "
        "and test_schematic_suffix_and_profile_routing."
    )
)
def test_schematic_end_to_end_extraction():
    raise AssertionError("unreachable: skipped")


# ---------------------------------------------------------------------------
# Technical orders (BOM + embedded figures) -> /v1/extract/technical-order
# ---------------------------------------------------------------------------

_TO_PAYLOAD = {
    "schema": BOM_SCHEMA_ID,
    "documentNumber": "TO 1C-130H-4",
    "documentType": "TO-IPB",
    "formatFamily": "mpl-modern",
    "extractionClass": "born-digital",
    "entryCount": 2,
    "figureCount": 1,
    "figures": [
        {
            "figureNumber": "3-1",
            "figureTitle": "Control Unit",
            "sheetNumber": "1",
            "pageNumber": 7,
            "mediaKey": "media/figure-3-1-sheet-1.png",
            "hotspots": [{"index": "1", "bbox": [10, 10, 30, 30]}],
        }
    ],
    "figureGroups": [
        {
            "figureNumber": "3-1",
            "figureTitle": "Control Unit",
            "sheetCount": 1,
            "declaredSheetTotal": None,
            "composition": "single",
            "sheets": [
                {
                    "sheetNumber": "1",
                    "pageNumber": 7,
                    "mediaKey": "media/figure-3-1-sheet-1.png",
                }
            ],
        }
    ],
    "bom": {"schema": BOM_SCHEMA_ID, "entries": [{}, {}], "figureGroups": []},
    "notes": [],
    "warnings": [],
}


@pytest.fixture
def fake_technical_order_extractor(monkeypatch):
    from docling_serve.technical_order import extract as to_extract

    calls: list[dict] = []

    def _fake(pdf_path, *, source_key="", media_dir=None, vision=None):
        calls.append(
            {
                "name": Path(pdf_path).name,
                "source_key": source_key,
                "media_dir": media_dir,
                "vision": vision,
            }
        )
        return dict(_TO_PAYLOAD)

    monkeypatch.setattr(to_extract, "extract_technical_order", _fake)
    return calls


async def test_extract_technical_order_route_contract(
    app, fake_technical_order_extractor
):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/technical-order",
            files=[("files", ("to-ipb.pdf", b"%PDF-1.4", "application/pdf"))],
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == BOM_SCHEMA_ID
    assert body["entryCount"] == 2
    assert body["figureCount"] == 1
    # Embedded figures + callout hotspots pass through untouched for the UI.
    assert body["figures"][0]["mediaKey"] == "media/figure-3-1-sheet-1.png"
    assert body["figures"][0]["hotspots"] == [{"index": "1", "bbox": [10, 10, 30, 30]}]
    assert body["figureGroups"][0]["composition"] == "single"
    assert body["bom"]["schema"] == BOM_SCHEMA_ID
    assert body["s3Keys"] == []

    call = fake_technical_order_extractor[-1]
    assert call["source_key"] == "to-ipb.pdf"
    assert call["media_dir"] is None  # inline path: no bundle media dir
    assert call["vision"] is None  # no Bedrock/LiteLLM configured -> no vision cfg


async def test_auto_technical_order_reuses_domain_execution(
    app, fake_technical_order_extractor
):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/auto",
            data={"profile": "technical-order"},
            files=[("files", ("to-ipb.pdf", b"%PDF-1.4", "application/pdf"))],
        )
    assert response.status_code == 200, response.text
    assert response.json()["routing"]["domain"] == "technical-order"
    assert fake_technical_order_extractor[-1]["source_key"] == "to-ipb.pdf"


async def test_extract_technical_order_route_rejects_non_pdf(app):
    async with _client(app) as client:
        response = await client.post(
            "/v1/extract/technical-order",
            files=[("files", ("to.xlsx", b"PK", "application/octet-stream"))],
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Legacy binary Office decks/spreadsheets
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "A real .doc/.ppt/.xls end-to-end fixture requires an installed LibreOffice "
        "runtime. Hermetic worker preconversion is covered with the converter "
        "mock in test_legacy_office.py; routing is covered above."
    )
)
def test_legacy_ppt_xls_conversion_end_to_end():
    raise AssertionError("unreachable: skipped")


# ---------------------------------------------------------------------------
# Auth wiring on the Wiki-facing routes
# ---------------------------------------------------------------------------


async def test_extraction_routes_require_api_key_when_configured(monkeypatch):
    """With an API key configured, the extraction surface Wiki calls must refuse
    unauthenticated requests (401) and let a valid key through to the route's
    own validation (422 for a wrong-suffix upload)."""
    from docling_serve import app as app_module

    monkeypatch.setattr(app_module.docling_serve_settings, "auth_mode", "api_key")
    monkeypatch.setattr(app_module.docling_serve_settings, "api_key", "wiki-secret")
    monkeypatch.setattr(app_module.docling_serve_settings, "allow_no_auth", False)
    with patch.object(app_module, "setup_otel_instrumentation"):
        app = app_module.create_app()

    files = [("files", ("report.pdf", b"%PDF-1.4", "application/pdf"))]
    async with _client(app) as client:
        unauthenticated = await client.post("/v1/extract/access", files=files)
        missing_tenant = await client.post(
            "/v1/extract/access", files=files, headers={"X-Api-Key": "wiki-secret"}
        )
        authenticated = await client.post(
            "/v1/extract/access",
            files=files,
            headers={"X-Api-Key": "wiki-secret", "X-Tenant-Id": "tenant-a"},
        )

    assert unauthenticated.status_code == 401
    assert missing_tenant.status_code == 400
    assert authenticated.status_code == 422  # auth passed; suffix check fired
