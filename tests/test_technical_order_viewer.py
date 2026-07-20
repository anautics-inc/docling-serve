"""The offline bundle viewer builder: data embedding and file layout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docling_serve.technical_order.viewer import build_viewer


def _minimal_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "media").mkdir(parents=True)
    bom = {
        "schema": "captify.bom.v1",
        "document": {
            "documentNumber": "35C2-2-33-51",
            "documentTitle": "TEST </script> SET",
        },
        "figures": [],
        "figureGroups": [],
        "entries": [],
        "stats": {"entryCount": 0, "figureCount": 0},
    }
    (bundle / "bom.json").write_text(json.dumps(bom), encoding="utf-8")
    return bundle


def test_build_viewer_writes_selfcontained_files(tmp_path):
    bundle = _minimal_bundle(tmp_path)
    index = build_viewer(bundle)

    assert index == bundle / "index.html"
    assert index.is_file()
    data = (bundle / "viewer-data.js").read_text(encoding="utf-8")
    assert data.startswith("window.CAPTIFY_BUNDLE = ")
    # A literal "</script" in document text must not terminate the data script.
    assert "</script" not in data
    assert "<\\/script" in data
    # The template loads data via script src, not fetch (file:// blocks fetch).
    html = index.read_text(encoding="utf-8")
    assert '<script src="viewer-data.js">' in html
    assert "fetch(" not in html


def test_build_viewer_copies_source_pdf(tmp_path):
    bundle = _minimal_bundle(tmp_path)
    pdf = tmp_path / "35C2-2-33-51.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    build_viewer(bundle, source_pdf=pdf)

    assert (bundle / "source" / "35C2-2-33-51.pdf").read_bytes() == b"%PDF-1.4 test"
    assert (
        '"sourcePdf":"source/35C2-2-33-51.pdf"'
        in (bundle / "viewer-data.js").read_text()
    )


def test_build_viewer_requires_bom(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_viewer(tmp_path)
