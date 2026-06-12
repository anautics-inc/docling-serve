"""Autonomous TO triage: the pipeline is told nothing about the file.

Derives, from the PDF alone:

- ``extraction_class`` — whether the embedded text layer can be trusted
  (born-digital) or must be re-OCR'd (scanned, or scanned with a dirty legacy
  OCR layer that silently corrupts part numbers).
- ``format_family`` — which parts-list grammar the document uses.
- ``document_kind`` — basic / merged-basic / change / supplement.
- ``document_type`` — TO-IPB / TO-RPSTL / other.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium

from docling_serve.extractors.technical_order.pdftext import page_layout_texts

#: Producers that indicate a born-digital AF TO toolchain.
_BORN_DIGITAL_PRODUCERS = re.compile(r"XPP|eBuild|Distiller|FrameMaker|XyEnterprise", re.I)

_MPL_HEADER = re.compile(r"FIGURE\s*&", re.I)
_MPL_CAGE_COLUMN = re.compile(r"\bCAGE\b")
_MPL_INDENTURE_RULER = re.compile(r"1\s?2\s?3\s?4\s?5\s?6\s?7")
_RPSTL_HEADER = re.compile(r"\bSMR\b.{0,40}\bCODE\b|\(UOC\)|USABLE ON CODE \(UOC\)", re.I)
_RPSTL_SERVICES = re.compile(r"\bARMY\b.{0,30}\b(AIR|FORCE)\b", re.S)
_CABLE_HEADER = re.compile(r"\bREF DES\b.{0,200}\bFROM\b.{0,80}\bTO\b", re.S)

_SUPPLEMENT = re.compile(r"^\s*SUPPLEMENT\s*$|This (publication|manual) supplements", re.I | re.M)
_MERGED = re.compile(r"BASIC AND ALL CHANGES\s+HAVE BEEN MERGED", re.I)
_CHANGE_LEVEL = re.compile(r"CHANGE\s+(\d+)\s*[-–—]", re.I)
_IPB_TITLE = re.compile(
    r"ILLUSTRATED PARTS BREAKDOWN|PARTS BREAKDOWN|PARTS LIST", re.I
)
_RPSTL_TITLE = re.compile(r"REPAIR PARTS AND SPECIAL TOOLS", re.I)


@dataclass(slots=True)
class TriageResult:
    extraction_class: str  # born-digital | scanned-no-text | scanned-dirty-ocr
    format_family: str  # mpl-modern | mpl-legacy | rpstl | ehb-cable | none | unknown
    document_kind: str  # basic | merged-basic | change | supplement | unknown
    document_type: str  # TO-IPB | TO-RPSTL | other
    page_count: int = 0
    producer: str = ""
    creator: str = ""
    median_chars_per_page: int = 0
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "extractionClass": self.extraction_class,
            "formatFamily": self.format_family,
            "documentKind": self.document_kind,
            "documentType": self.document_type,
            "pageCount": self.page_count,
            "producer": self.producer,
            "creator": self.creator,
            "medianCharsPerPage": self.median_chars_per_page,
            "signals": self.signals,
        }


def _sample_page_numbers(page_count: int) -> list[int]:
    """Title pages plus mid-document pages (parts lists live in the back half)."""
    sample = {1, 2, 3}
    for frac in (0.4, 0.6, 0.8):
        sample.add(max(1, min(page_count, round(page_count * frac))))
    return sorted(p for p in sample if p <= page_count)


def triage_pdf(pdf_path: Path) -> TriageResult:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(pdf)
        meta = pdf.get_metadata_dict()
    finally:
        pdf.close()
    producer = (meta.get("Producer") or "").strip()
    creator = (meta.get("Creator") or "").strip()

    signals: list[str] = []
    sample_pages = _sample_page_numbers(page_count)
    sampled: list[str] = []
    for p in sample_pages:
        try:
            sampled.extend(page_layout_texts(pdf_path, first=p, last=p))
        except Exception:
            sampled.append("")
    char_counts = [len(re.sub(r"\s", "", t)) for t in sampled]
    median_chars = int(statistics.median(char_counts)) if char_counts else 0
    body_counts = char_counts[3:] or char_counts
    body_median = int(statistics.median(body_counts)) if body_counts else 0

    born_digital_producer = bool(
        _BORN_DIGITAL_PRODUCERS.search(producer) or _BORN_DIGITAL_PRODUCERS.search(creator)
    )
    if body_median < 40:
        extraction_class = "scanned-no-text"
        signals.append(f"body median chars/page={body_median}")
    elif born_digital_producer:
        extraction_class = "born-digital"
        signals.append(f"producer={producer or creator}")
    else:
        # Text exists but the toolchain is unknown: a scan with an embedded
        # legacy OCR layer is indistinguishable from clean text by volume, so
        # never trust it blindly.
        extraction_class = "scanned-dirty-ocr"
        signals.append("text present but producer not a known born-digital toolchain")

    text = "\n".join(sampled)
    format_family = "unknown"
    if _MPL_HEADER.search(text):
        header_zone = _header_zone(text)
        if _MPL_CAGE_COLUMN.search(header_zone):
            format_family = "mpl-modern"
        else:
            format_family = "mpl-legacy"
    elif _RPSTL_HEADER.search(text) and _RPSTL_SERVICES.search(text):
        format_family = "rpstl"
    elif _CABLE_HEADER.search(text):
        format_family = "ehb-cable"
    elif extraction_class == "scanned-no-text":
        format_family = "unknown"
    else:
        format_family = "none"

    title_text = "\n".join(sampled[:3])
    if _SUPPLEMENT.search(title_text):
        document_kind = "supplement"
    elif _MERGED.search(title_text):
        document_kind = "merged-basic"
    elif _CHANGE_LEVEL.search(title_text):
        document_kind = "merged-basic"
        signals.append("change level on title page")
    elif extraction_class == "scanned-no-text":
        document_kind = "unknown"
    else:
        document_kind = "basic"

    if _RPSTL_TITLE.search(title_text):
        document_type = "TO-RPSTL"
    elif _IPB_TITLE.search(text):
        document_type = "TO-IPB"
    else:
        document_type = "other"

    return TriageResult(
        extraction_class=extraction_class,
        format_family=format_family,
        document_kind=document_kind,
        document_type=document_type,
        page_count=page_count,
        producer=producer,
        creator=creator,
        median_chars_per_page=median_chars,
        signals=signals,
    )


def _header_zone(text: str) -> str:
    """Lines near MPL header blocks, where the CAGE column header would be."""
    lines = text.splitlines()
    zone: list[str] = []
    for i, line in enumerate(lines):
        if _MPL_HEADER.search(line):
            zone.extend(lines[i : i + 4])
    return "\n".join(zone)
