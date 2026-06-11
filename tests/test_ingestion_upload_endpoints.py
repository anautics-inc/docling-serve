"""Async chunk-endpoint tests for the ingestion upload paths (issues A1 + A2).

Exercises the full in-process pipeline — endpoint -> local orchestrator ->
worker -> chunk processing — for:

- a legacy ``.doc`` upload pre-converted via LibreOffice (soffice mocked with
  real ``.docx`` fixture bytes when LibreOffice is unavailable),
- a corrupt legacy upload failing with a typed error naming the conversion,
- an Access ``.mdb`` upload recovered by the AccessExtractor (mdbtools mocked
  at the subprocess boundary),
- a modern ``.docx`` upload (no behavior change).

Uses the hierarchical chunker endpoint so no tokenizer download is needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

import docling_serve.app as app_module
from docling_serve.app import create_app
from docling_serve.extractors import access_extractor as access_mod
from docling_serve.settings import docling_serve_settings

TEST_FILES = Path(__file__).parent / "test_files"
DOCX_FIXTURE = TEST_FILES / "generated-code-validation-procedures.docx"

CHUNK_ENDPOINT = "/v1/chunk/hierarchical/file/async"
POLL_TIMEOUT_SECONDS = 120


@pytest.fixture(scope="session")
def event_loop():
    return asyncio.get_event_loop()


@pytest.fixture(scope="session")
def auth_headers():
    headers = {}
    if docling_serve_settings.api_key:
        headers["X-Api-Key"] = docling_serve_settings.api_key
    return headers


@pytest_asyncio.fixture(scope="session")
async def app():
    # The chunk paths under test never need the heavy conversion models.
    docling_serve_settings.load_models_at_boot = False
    app = create_app()
    async with LifespanManager(app) as manager:
        yield manager.app


@pytest_asyncio.fixture(scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://app.io"
    ) as client:
        yield client


async def _run_chunk_task(client: AsyncClient, headers: dict, files: dict) -> dict:
    """Submit files to the async chunk endpoint, poll, and return the result."""
    response = await client.post(CHUNK_ENDPOINT, files=files, headers=headers)
    assert response.status_code == 200, response.text
    task = response.json()

    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_SECONDS
    while task["task_status"] not in ("success", "failure"):
        assert asyncio.get_event_loop().time() < deadline, "task polling timed out"
        await asyncio.sleep(0.5)
        response = await client.get(
            f"/v1/status/poll/{task['task_id']}", headers=headers
        )
        assert response.status_code == 200, response.text
        task = response.json()

    assert task["task_status"] == "success", task.get("error_message")
    result = await client.get(f"/v1/result/{task['task_id']}", headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.asyncio
async def test_legacy_doc_upload_chunks_end_to_end(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    """A .doc upload completes the chunk pipeline with text chunks (A1).

    The LibreOffice boundary is mocked with real .docx fixture bytes so the
    test validates the wiring (pre-conversion -> docling -> chunks -> original
    filename restore) without soffice installed.
    """

    def _fake_convert(name: str, data: bytes) -> tuple[str, bytes]:
        assert name == "procedures.doc"
        return "procedures.docx", DOCX_FIXTURE.read_bytes()

    monkeypatch.setattr(app_module, "convert_legacy_office_bytes", _fake_convert)

    data = await _run_chunk_task(
        client,
        auth_headers,
        files={"files": ("procedures.doc", b"legacy-doc-bytes", "application/msword")},
    )

    chunks = data["chunks"]
    assert chunks, "legacy .doc upload produced no chunks"
    assert any(chunk["text"].strip() for chunk in chunks)
    # The original (legacy) filename is preserved in the result metadata.
    assert {chunk["filename"] for chunk in chunks} == {"procedures.doc"}
    assert [doc["content"]["filename"] for doc in data["documents"]] == [
        "procedures.doc"
    ]


@pytest.mark.asyncio
async def test_corrupt_legacy_upload_fails_with_typed_error(
    client: AsyncClient, auth_headers: dict
):
    """A corrupt legacy file fails with a typed error naming the conversion (A1).

    Without LibreOffice installed the pre-conversion step reports it is
    unavailable; with LibreOffice the corrupt bytes fail the conversion. Both
    are LegacyOfficeConversionError -> 422 naming LibreOffice, never a generic
    docling failure.
    """
    response = await client.post(
        CHUNK_ENDPOINT,
        files={"files": ("corrupt.doc", b"\x00\x01 not a doc", "application/msword")},
        headers=auth_headers,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "LibreOffice" in detail
    assert "corrupt.doc" in detail


@pytest.mark.asyncio
async def test_access_mdb_upload_yields_per_table_chunks(
    client: AsyncClient, auth_headers: dict, monkeypatch
):
    """An .mdb upload completes the async chunk pipeline with table chunks (A2)."""
    monkeypatch.setattr(access_mod, "mdbtools_available", lambda: True)
    monkeypatch.setattr(access_mod, "list_tables", lambda p: ["Parts", "Orders"])
    monkeypatch.setattr(
        access_mod,
        "export_table",
        lambda p, t: (["id", "name"], [["1", f"{t.lower()}-row"]]),
    )
    monkeypatch.setattr(access_mod, "dump_schema", lambda p: "CREATE TABLE Parts (...);")

    data = await _run_chunk_task(
        client,
        auth_headers,
        files={
            "files": ("inventory.mdb", b"fake-access-bytes", "application/octet-stream")
        },
    )

    chunks = data["chunks"]
    assert [chunk["headings"] for chunk in chunks] == [["Parts"], ["Orders"]]
    assert {chunk["filename"] for chunk in chunks} == {"inventory.mdb"}
    for chunk in chunks:
        assert "id\tname" in chunk["text"]  # column-header context
        assert chunk["metadata"]["domain"] == "database"
        assert chunk["metadata"]["extractor"] == "extract_access"
    assert [doc["content"]["filename"] for doc in data["documents"]] == [
        "inventory.mdb"
    ]
    assert data["documents"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_modern_docx_upload_unchanged(client: AsyncClient, auth_headers: dict):
    """A modern .docx upload chunks exactly as before (A1: no behavior change)."""
    data = await _run_chunk_task(
        client,
        auth_headers,
        files={
            "files": (
                DOCX_FIXTURE.name,
                DOCX_FIXTURE.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    chunks = data["chunks"]
    assert chunks
    assert {chunk["filename"] for chunk in chunks} == {DOCX_FIXTURE.name}
    assert any(chunk["text"].strip() for chunk in chunks)
