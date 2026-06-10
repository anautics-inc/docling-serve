"""Unit tests for the XFA form extractor.

Uses the real AFMC MP5327.9001 Market Research Report fixture (an Adobe
LiveCycle / AF e-Publishing dynamic form) — no network, no models.
"""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf
import pytest

from docling_serve.extractors import ExtractionContext, select_extractor
from docling_serve.extractors.xfa_extractor import (
    XfaFormExtractor,
    pdf_has_xfa,
    to_mm,
)

FIXTURE = Path(__file__).parent / "test_files" / "AFMC MP5327.9001market_research_report.pdf"


def _ctx(tmp_path: Path, source: Path) -> ExtractionContext:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    return ExtractionContext(
        source_path=source,
        bundle_dir=bundle,
        media_dir=bundle / "media",
        source_manifest_key=f"task:test:{source.stem}",
        task_id="test",
    )


def test_to_mm_converts_xfa_measurements() -> None:
    assert to_mm("12.7mm") == 12.7
    assert to_mm("0.5in") == 12.7
    assert to_mm("72pt") == 25.4
    assert to_mm("1cm") == 10.0
    assert to_mm(None) is None
    assert to_mm("garbage") is None


def test_xfa_pdf_is_detected_and_selected(tmp_path: Path) -> None:
    assert pdf_has_xfa(FIXTURE) is True
    extractor = select_extractor(_ctx(tmp_path, FIXTURE))
    assert extractor.name == "extract_xfa_form"


def test_plain_pdf_is_not_claimed(tmp_path: Path) -> None:
    plain = tmp_path / "plain.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(plain)
    assert pdf_has_xfa(plain) is False
    extractor = select_extractor(_ctx(tmp_path, plain))
    assert extractor.name != "extract_xfa_form"


def test_build_extracts_fields_sections_and_coordinates(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, FIXTURE)
    result = XfaFormExtractor().build(ctx)

    assert result.extractor == "extract_xfa_form"
    assert result.domain == "form"
    assert set(result.artifacts) >= {"xfa-template.xml", "xfa-datasets.xml", "xfa-fields.json"}

    catalog = json.loads((ctx.bundle_dir / "xfa-fields.json").read_text())
    assert catalog["fieldCount"] > 50
    assert catalog["boundValueCount"] > 0

    fields = catalog["fields"]
    by_name = {f["name"]: f for f in fields if f["kind"] == "field"}
    # Section A general-contract-info entries exist with full dotted paths.
    assert "PR-ID_entry" in by_name
    assert by_name["PR-ID_entry"]["path"].startswith("form1.Page1.SectionA")
    assert by_name["NAICS_entry"]["section"] == "SectionA"
    # UI types are captured (text vs date widgets).
    assert by_name["Report_Date_entry"]["uiType"] == "dateTimeEdit"
    # Coordinates: every record carries a bbox; positioned widgets have mm coords.
    assert all("bbox" in f for f in fields)
    positioned = [f for f in fields if f["bbox"]["absXmm"] is not None]
    assert positioned, "expected at least some absolutely positioned widgets"
    assert all(isinstance(f["bbox"]["absXmm"], float) for f in positioned)
    # Static labels (captions) are captured for grounding.
    labels = [f["text"] for f in fields if f["kind"] == "label" and f.get("text")]
    assert any("Section A" in label for label in labels)

    # Deep-document units: one per form section, elements carry bbox + type.
    units = result.structured["document"]["units"]
    titles = {u.get("title") for u in units}
    assert {"SectionA", "SectionB"} <= titles
    assert result.structured["form"]["format"] == "xfa"


def test_profile_forces_xfa_extractor(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, FIXTURE)
    ctx.profile = "xfa"
    assert select_extractor(ctx).name == "extract_xfa_form"
