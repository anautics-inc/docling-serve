"""XFA (XML Forms Architecture) form extractor via pikepdf.

Air Force / DoD e-Publishing "dynamic" forms (Adobe LiveCycle, e.g. the AFMC
MP5327.9001 Market Research Report) are XFA PDFs: the PDF page tree is only the
"Please wait…" placeholder and the real form lives in document-level XML packets
under ``Root.AcroForm.XFA`` (``template`` = form structure, ``datasets`` = data).
Docling's PDF pipeline (and every text-layer/OCR pass) cannot see any of it, so
this extractor bypasses the conversion result and reads the XFA packets directly
with ``pikepdf``.

What it produces per form:

- ``document.json`` units — one unit per form section (``SectionA`` …), whose
  elements are the section's **fields and static labels** with type, caption,
  bound value, choice options, and a ``bbox`` carrying both the raw XFA layout
  attributes and the **absolute x/y position in millimetres** (accumulated from
  positioned ancestor subforms).
- Sidecars: ``xfa-template.xml`` + ``xfa-datasets.xml`` (the raw packets) and
  ``xfa-fields.json`` — the flat field catalog downstream form-fill / agent
  pipelines consume.

Detection is content-based (``supports`` peeks for an ``/XFA`` key), so any XFA
PDF routes here automatically — like .pptx → PptxExtractor — with the Docling
extractor untouched for ordinary PDFs.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

# Parse untrusted document XML with defusedxml to block entity-expansion
# ("billion laughs") and external-entity attacks. ``ET`` is kept only for the
# (safe) Element type annotations used below.
from defusedxml.ElementTree import fromstring as _safe_fromstring

from docling_serve.deep_document.artifact_writer import write_json
from docling_serve.deep_document.document_builder import build_deep_document
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)

_log = logging.getLogger(__name__)

XFA_PROFILES = {"xfa", "form", "af-form", "dod-form"}

# XFA measurement units → millimetres.
_UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0}
_MEASUREMENT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(mm|cm|in|pt)?$")

# Template packet structural elements we recurse through with layout offsets.
_CONTAINER_TAGS = {"subform", "subformSet", "area", "exclGroup", "pageArea", "contentArea"}


class XfaToolsUnavailableError(RuntimeError):
    """Raised when pikepdf is not installed."""


def _pikepdf() -> Any:
    try:
        import pikepdf
    except ImportError as error:  # pragma: no cover - environment-dependent
        raise XfaToolsUnavailableError(
            "pikepdf is required for XFA form extraction (uv pip install pikepdf)."
        ) from error
    return pikepdf


def pdf_has_xfa(path: Path) -> bool:
    """Cheap content check: does this PDF carry an XFA packet array?"""
    try:
        pikepdf = _pikepdf()
        with pikepdf.open(path) as pdf:
            acroform = pdf.Root.get("/AcroForm")
            return acroform is not None and acroform.get("/XFA") is not None
    except XfaToolsUnavailableError:
        raise
    except Exception:
        return False


def read_xfa_packets(path: Path) -> dict[str, bytes]:
    """Return the named XFA packets (``template``, ``datasets``, …)."""
    pikepdf = _pikepdf()
    with pikepdf.open(path) as pdf:
        xfa = pdf.Root.AcroForm.XFA
        packets: dict[str, bytes] = {}
        for index in range(0, len(xfa) - 1, 2):
            name = str(xfa[index]).strip("/")
            if name.startswith(("xdp", "</")):
                continue
            try:
                packets[name] = xfa[index + 1].read_bytes()
            except Exception:
                _log.warning("XFA packet %s could not be read", name)
        return packets


def to_mm(value: str | None) -> float | None:
    """Parse an XFA measurement ('12.7mm', '0.5in', '36pt') into millimetres."""
    if not value:
        return None
    match = _MEASUREMENT_RE.match(value.strip())
    if not match:
        return None
    return round(float(match.group(1)) * _UNIT_TO_MM.get(match.group(2) or "mm", 1.0), 3)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(element: ET.Element | None) -> str:
    """All text under an element (handles xhtml-rich exData bodies)."""
    if element is None:
        return ""
    return re.sub(r"\s+", " ", " ".join(element.itertext())).strip()


def _first_child(element: ET.Element, tag: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) == tag:
            return child
    return None


@dataclass(slots=True)
class _Walker:
    """Recursive template walk accumulating positioned-ancestor offsets."""

    fields: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def walk(
        self,
        element: ET.Element,
        path: list[str],
        offset_x: float,
        offset_y: float,
        page: str | None,
        section: str | None,
    ) -> None:
        for child in element:
            tag = _local(child.tag)
            name = child.get("name")
            if tag in _CONTAINER_TAGS:
                child_x = to_mm(child.get("x")) or 0.0
                child_y = to_mm(child.get("y")) or 0.0
                child_path = [*path, name] if name else path
                child_page = page
                child_section = section
                if name and name.lower().startswith("page"):
                    child_page = name
                elif name and page is not None and section is None:
                    # First named subform below the page level is the section.
                    child_section = name
                self.walk(
                    child,
                    child_path,
                    offset_x + child_x,
                    offset_y + child_y,
                    child_page,
                    child_section,
                )
            elif tag in ("field", "draw"):
                self.fields.append(
                    self._capture(child, tag, path, offset_x, offset_y, page, section)
                )

    def _capture(
        self,
        element: ET.Element,
        kind: str,
        path: list[str],
        offset_x: float,
        offset_y: float,
        page: str | None,
        section: str | None,
    ) -> dict[str, Any]:
        name = element.get("name") or kind
        x = to_mm(element.get("x"))
        y = to_mm(element.get("y"))
        record: dict[str, Any] = {
            "name": name,
            "path": ".".join([*path, name]),
            "kind": "label" if kind == "draw" else "field",
            "page": page,
            "section": section,
            "uiType": self._ui_type(element),
            "caption": self._caption(element),
            "value": None,
            "options": self._options(element),
            "bbox": {
                "x": element.get("x"),
                "y": element.get("y"),
                "w": element.get("w"),
                "h": element.get("h"),
                "absXmm": round(offset_x + x, 3) if x is not None else None,
                "absYmm": round(offset_y + y, 3) if y is not None else None,
                "wMm": to_mm(element.get("w")),
                "hMm": to_mm(element.get("h")),
                "unit": "mm",
            },
        }
        value = _first_child(element, "value")
        if value is not None:
            record["value"] = _text_of(value) or None
        if kind == "draw":
            record["text"] = record.pop("value", None) or record["caption"]
        return record

    @staticmethod
    def _ui_type(element: ET.Element) -> str | None:
        ui = _first_child(element, "ui")
        if ui is None:
            return None
        for child in ui:
            tag = _local(child.tag)
            if tag != "extras":
                return tag
        return None

    @staticmethod
    def _caption(element: ET.Element) -> str | None:
        caption = _first_child(element, "caption")
        return (_text_of(caption) or None) if caption is not None else None

    @staticmethod
    def _options(element: ET.Element) -> list[str]:
        options: list[str] = []
        for child in element:
            if _local(child.tag) == "items":
                options.extend(
                    text
                    for item in child
                    if (text := _text_of(item))
                )
        return options


def parse_template_fields(template_xml: bytes) -> list[dict[str, Any]]:
    """All fields + static labels in the template, with absolute mm coordinates."""
    root = _safe_fromstring(template_xml)
    walker = _Walker()
    walker.walk(root, [], 0.0, 0.0, None, None)
    return walker.fields


def parse_dataset_values(datasets_xml: bytes) -> dict[str, str]:
    """Flatten ``xfa:datasets/xfa:data`` into ``dotted.path -> text`` (leaves only)."""
    root = _safe_fromstring(datasets_xml)
    data = next((child for child in root if _local(child.tag) == "data"), root)
    values: dict[str, str] = {}

    def descend(element: ET.Element, path: list[str]) -> None:
        children = list(element)
        if not children:
            text = _text_of(element)
            if text:
                values[".".join(path)] = text
            return
        for child in children:
            descend(child, [*path, _local(child.tag)])

    for child in data:
        descend(child, [_local(child.tag)])
    return values


def merge_values(fields: list[dict[str, Any]], values: dict[str, str]) -> int:
    """Bind dataset values onto fields by longest matching dotted-path suffix."""
    bound = 0
    for record in fields:
        if record["kind"] != "field":
            continue
        path = record["path"]
        candidates = [
            value
            for data_path, value in values.items()
            if path.endswith(data_path) or data_path.endswith(path.split(".", 1)[-1])
        ]
        if candidates:
            record["boundValue"] = candidates[0]
            bound += 1
    return bound


class XfaFormExtractor(Extractor):
    name = "extract_xfa_form"

    def supports(self, ctx: ExtractionContext) -> bool:
        if (ctx.profile or "").strip().lower() in XFA_PROFILES:
            return True
        source = ctx.resolve_source_file()
        if source.suffix.lower() != ".pdf" or not source.is_file():
            return False
        return pdf_has_xfa(source)

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        source_file = ctx.resolve_source_file()
        packets = read_xfa_packets(source_file)
        template = packets.get("template")
        if not template:
            raise RuntimeError(f"XFA PDF has no template packet: {source_file.name}")

        fields = parse_template_fields(template)
        bound_count = 0
        if packets.get("datasets"):
            bound_count = merge_values(fields, parse_dataset_values(packets["datasets"]))

        artifacts: list[str] = []
        for packet_name in ("template", "datasets"):
            payload = packets.get(packet_name)
            if payload:
                sidecar = ctx.bundle_dir / f"xfa-{packet_name}.xml"
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_bytes(payload)
                artifacts.append(sidecar.relative_to(ctx.bundle_dir).as_posix())

        catalog_path = ctx.bundle_dir / "xfa-fields.json"
        write_json(
            catalog_path,
            {
                "source": ctx.source_path.name,
                "fieldCount": sum(1 for f in fields if f["kind"] == "field"),
                "labelCount": sum(1 for f in fields if f["kind"] == "label"),
                "boundValueCount": bound_count,
                "fields": fields,
            },
        )
        artifacts.append(catalog_path.relative_to(ctx.bundle_dir).as_posix())

        units = self._units(fields)
        manifest = {
            "artifactKind": "captify.xfa.normalizedExtraction.v1",
            "source": {
                "originalFileName": ctx.source_path.name,
                "fileKind": "form",
                "sourceManifestKey": ctx.source_manifest_key,
            },
            "units": units,
            "assets": [],
        }
        structured = build_deep_document(
            manifest=manifest, source_manifest_key=ctx.source_manifest_key
        )
        structured["form"] = {
            "format": "xfa",
            "fieldCount": sum(1 for f in fields if f["kind"] == "field"),
            "sections": sorted({f["section"] for f in fields if f["section"]}),
            "fieldCatalog": "xfa-fields.json",
        }
        validate_artifact(structured, "deep-document.schema.json")

        return ExtractorResult(
            structured=structured,
            extractor=self.name,
            domain="form",
            artifacts=artifacts,
            manifest_extra={
                "form": {
                    "format": "xfa",
                    "fieldCount": structured["form"]["fieldCount"],
                    "boundValueCount": bound_count,
                    "fieldCatalog": "xfa-fields.json",
                    "template": "xfa-template.xml",
                    "datasets": "xfa-datasets.xml" if packets.get("datasets") else None,
                }
            },
            notes=[
                f"XFA form: {structured['form']['fieldCount']} fields across "
                f"{len(structured['form']['sections'])} sections; {bound_count} bound values."
            ],
        )

    @staticmethod
    def _units(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One unit per form section, elements = the section's fields/labels."""
        by_section: dict[str, list[dict[str, Any]]] = {}
        for record in fields:
            by_section.setdefault(record["section"] or record["page"] or "form", []).append(record)

        units: list[dict[str, Any]] = []
        for index, (section, records) in enumerate(by_section.items()):
            elements = []
            for element_index, record in enumerate(records):
                text = record.get("boundValue") or record.get("value") or record.get("text") or ""
                caption = record.get("caption") or ""
                plain = ": ".join(part for part in (caption or record["name"], text) if part)
                elements.append(
                    {
                        "elementId": f"unit-{index + 1:04d}-element-{element_index + 1:04d}",
                        "type": "form_field" if record["kind"] == "field" else "label",
                        "kind": record.get("uiType") or record["kind"],
                        "name": record["name"],
                        "path": record["path"],
                        "page": record["page"],
                        "caption": record.get("caption"),
                        "options": record.get("options") or [],
                        "boundValue": record.get("boundValue"),
                        "bbox": record["bbox"],
                        "text": {"plain": plain, "paragraphs": [], "runs": []},
                    }
                )
            units.append(
                {
                    "unitId": f"unit-{index + 1:04d}",
                    "unitType": "form_section",
                    "index": index,
                    "title": section,
                    "sourceRefs": {"section": section},
                    "elements": elements,
                }
            )
        return units
