from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from docling.datamodel.base_models import OutputFormat
from docling.datamodel.document import ConversionStatus
from docling_core.types.doc import DoclingDocument
from docling_jobkit.datamodel.result import RemoteTargetResult

from docling_serve.deep_document import export_results
from docling_serve.settings import docling_serve_settings


def _conversion_result(filename: str = "fixture.pdf") -> SimpleNamespace:
    document = DoclingDocument(name=Path(filename).stem)
    document.add_text(label="title", text="AFTO Form 874 Procedure")
    document.add_text(
        label="text",
        text="Complete AFTO Form 874 using the source procedure.",
    )
    return SimpleNamespace(
        status=ConversionStatus.SUCCESS,
        document=document,
        input=SimpleNamespace(file=Path(filename)),
    )


def _convert_options(*formats: OutputFormat) -> SimpleNamespace:
    return SimpleNamespace(
        to_formats=list(formats),
        image_export_mode=None,
        md_page_break_placeholder=None,
    )


def test_deep_export_delegates_to_jobkit_for_default_extraction(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = {}

    def fake_process_export_results(**kwargs):
        calls.update(kwargs)
        return "delegated"

    monkeypatch.setattr(
        export_results, "process_export_results", fake_process_export_results
    )

    result = export_results.process_export_results_with_deep_document(
        task=SimpleNamespace(metadata={"extraction": "default"}),
        conv_results=[],
        work_dir=tmp_path,
    )

    assert result == "delegated"
    assert calls["conv_results"] == []
    assert calls["work_dir"] == tmp_path


def test_deep_export_fails_explicitly_without_any_bucket(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # No server default bucket and no request-supplied bucket.
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "")

    with pytest.raises(RuntimeError, match="S3 bucket"):
        export_results.process_export_results_with_deep_document(
            task=SimpleNamespace(
                task_id="task-001",
                target=SimpleNamespace(),
                sources=[SimpleNamespace()],
                callbacks=[],
                metadata={"extraction": "deep"},
                convert_options=_convert_options(OutputFormat.JSON),
            ),
            conv_results=[_conversion_result()],
            work_dir=tmp_path,
        )


def test_deep_export_uses_request_supplied_bucket(
    monkeypatch,
    tmp_path: Path,
) -> None:
    uploads = {}

    def fake_export_documents_as_files(**kwargs):
        (kwargs["output_dir"] / "fixture.json").write_text("{}\n")
        return (1, 0, None)

    def fake_upload_tree(**kwargs):
        uploads.update(kwargs)
        return []

    # Server has no default bucket — the request must supply it, and a
    # request-supplied bucket must be on the explicit allow-list
    # (confused-deputy guard in ensure_bucket_allowed).
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "")
    monkeypatch.setattr(
        docling_serve_settings,
        "deep_document_s3_allowed_buckets",
        ["app-documents"],
    )
    monkeypatch.setattr(
        export_results, "_export_documents_as_files", fake_export_documents_as_files
    )
    monkeypatch.setattr(export_results, "upload_tree", fake_upload_tree)

    result = export_results.process_export_results_with_deep_document(
        task=SimpleNamespace(
            task_id="task-001",
            target=SimpleNamespace(),
            sources=[SimpleNamespace()],
            callbacks=[],
            metadata={
                "extraction": "deep",
                "deep_s3_bucket": "app-documents",
                "deep_s3_prefix": "documents/doc-42/docling",
            },
            convert_options=_convert_options(OutputFormat.JSON),
        ),
        conv_results=[_conversion_result()],
        work_dir=tmp_path,
    )

    assert isinstance(result.result, RemoteTargetResult)
    assert uploads["bucket"] == "app-documents"
    assert uploads["prefix"] == "documents/doc-42/docling"

    # One standard bundle is assembled at the prefix root (single document).
    assert (tmp_path / "output" / "extraction.json").exists()
    assert (tmp_path / "output" / "document.json").exists()


def test_deep_export_publishes_expanded_s3_tree(
    monkeypatch,
    tmp_path: Path,
) -> None:
    uploads = {}

    def fake_export_documents_as_files(**kwargs):
        output_dir = kwargs["output_dir"]
        (output_dir / "fixture.json").write_text("{}\n")
        (output_dir / "fixture.html").write_text("<html></html>\n")
        (output_dir / "fixture.md").write_text("# Fixture\n")
        (output_dir / "artifacts").mkdir()
        (output_dir / "artifacts" / "figure.png").write_bytes(b"png")
        return (1, 0, None)

    def fake_upload_tree(**kwargs):
        uploads.update(kwargs)
        return []

    monkeypatch.setattr(
        docling_serve_settings, "deep_document_s3_bucket", "captify-documents"
    )
    monkeypatch.setattr(
        docling_serve_settings,
        "deep_document_s3_prefix_template",
        "documents/{tenant_id}/docling/{task_id}",
    )
    monkeypatch.setattr(
        export_results, "_export_documents_as_files", fake_export_documents_as_files
    )
    monkeypatch.setattr(export_results, "upload_tree", fake_upload_tree)

    result = export_results.process_export_results_with_deep_document(
        task=SimpleNamespace(
            task_id="task-001",
            target=SimpleNamespace(),
            sources=[SimpleNamespace()],
            callbacks=[],
            metadata={"extraction": "deep", "tenant_id": "acme"},
            convert_options=_convert_options(
                OutputFormat.JSON, OutputFormat.HTML, OutputFormat.MARKDOWN
            ),
        ),
        conv_results=[_conversion_result()],
        work_dir=tmp_path,
    )

    assert result.num_succeeded == 1
    # Deep extraction returns a remote-target result — no ZIP in the body.
    assert isinstance(result.result, RemoteTargetResult)
    assert uploads["bucket"] == "captify-documents"
    assert uploads["prefix"] == "documents/acme/docling/task-001"

    # The standard bundle: reassemblable document + manifest + media tree.
    output = tmp_path / "output"
    assert (output / "document.json").exists()
    assert (output / "document.md").exists()
    assert (output / "document.html").exists()
    assert (output / "media" / "figure.png").exists()

    manifest = json.loads((output / "extraction.json").read_text())
    assert manifest["taskId"] == "task-001"
    assert any(f.endswith("figure.png") for f in manifest.get("media", [])) or (
        output / "media" / "figure.png"
    ).exists()
