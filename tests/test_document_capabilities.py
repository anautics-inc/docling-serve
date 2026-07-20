from __future__ import annotations

import zlib

import pytest

from docling_serve.capabilities import (
    CAPABILITIES,
    OcrPolicy,
    capability_for_filename,
    classify_document,
    parse_ocr_policy,
)


def test_capability_matrix_covers_every_ingestion_family():
    assert set(CAPABILITIES) == {
        "document",
        "legacy-office",
        "access",
        "form",
        "technical-order",
        "schematic",
        "graph-extraction",
    }
    generic = CAPABILITIES["document"].extensions
    assert {
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".html",
        ".htm",
        ".md",
        ".txt",
        ".adoc",
        ".asciidoc",
        ".csv",
        ".png",
        ".jpg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
    } <= generic
    assert CAPABILITIES["legacy-office"].extensions == {".doc", ".ppt", ".xls"}
    assert CAPABILITIES["access"].extensions == {".mdb", ".accdb"}
    assert CAPABILITIES["form"].output_contract == "captify.form.v1"
    assert CAPABILITIES["technical-order"].output_contract == "captify.bom.v2"
    assert CAPABILITIES["schematic"].output_contract == "captify.schematic.v1"
    assert CAPABILITIES["graph-extraction"].extensions == frozenset()
    assert CAPABILITIES["graph-extraction"].output_contract == (
        "captify.graph-extraction.v1"
    )


@pytest.mark.parametrize(
    ("filename", "domain"),
    [
        ("report.pdf", "document"),
        ("notes.docx", "document"),
        ("deck.pptx", "document"),
        ("sheet.xlsx", "document"),
        ("scan.png", "document"),
        ("old.doc", "legacy-office"),
        ("old.ppt", "legacy-office"),
        ("old.xls", "legacy-office"),
        ("inventory.mdb", "access"),
    ],
)
def test_filename_admission_uses_registry(filename, domain):
    capability = capability_for_filename(filename)
    assert capability is not None
    assert capability.name == domain


def test_ocr_policy_has_typed_and_legacy_compatibility():
    assert parse_ocr_policy(None) is OcrPolicy.AUTO
    assert parse_ocr_policy(None, legacy_do_ocr=True) is OcrPolicy.ALWAYS
    assert parse_ocr_policy(None, legacy_do_ocr=False) is OcrPolicy.NEVER
    assert parse_ocr_policy("auto", legacy_do_ocr=True) is OcrPolicy.AUTO
    with pytest.raises(ValueError, match="auto, always, never"):
        parse_ocr_policy("sometimes")


def test_explicit_profile_wins_over_content_probes():
    decision = classify_document(
        filename="generic.pdf",
        payload=b"%PDF /XFA",
        profile="schematic",
        ocr_policy="never",
    )
    assert decision.public_dict() == {
        "domain": "schematic",
        "reason": "explicit profile",
        "ocrPolicy": "never",
    }


def test_auto_routes_access_xfa_technical_order_and_generic():
    assert classify_document(filename="db.accdb", payload=b"db").domain == "access"
    assert classify_document(filename="form.pdf", payload=b"%PDF /XFA").domain == "form"
    assert (
        classify_document(
            filename="manual.pdf",
            payload=b"%PDF",
            markdown="FIGURE & INDEX\nPART NUMBER\nSMR CODE",
        ).domain
        == "technical-order"
    )
    assert classify_document(filename="notes.pdf", payload=b"%PDF").domain == "document"


def test_vector_probe_is_bounded_and_routes_schematic():
    drawing = b" ".join([b"1 1 l 2 2 c"] * 101)
    payload = b"%PDF\nstream\n" + zlib.compress(drawing) + b"\nendstream"
    assert (
        classify_document(filename="drawing.pdf", payload=payload).domain == "schematic"
    )
    raster = payload + b"/Subtype /Image"
    assert classify_document(filename="scan.pdf", payload=raster).domain == "document"


def test_unknown_format_fails_before_processing():
    with pytest.raises(ValueError, match="Unsupported document format"):
        classify_document(filename="payload.exe", payload=b"MZ")
