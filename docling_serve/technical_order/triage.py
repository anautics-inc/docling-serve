"""Autonomous TO triage: the pipeline is told nothing about the file.

Derives, from the PDF alone:

- ``extraction_class`` — whether the embedded text layer can be trusted
  (born-digital) or must be re-OCR'd (scanned, or scanned with a dirty legacy
  OCR layer that silently corrupts part numbers).
- ``format_family`` — which parts-list grammar the document uses.
- ``document_kind`` — basic / merged-basic / change / supplement.
- ``document_type`` — TO-IPB / TO-RPSTL / other.
- ``ocr_ready`` — whether the OCR fallback in :mod:`extract` can actually run
  for this document (tesseract present, page count within budget).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium

from docling_serve.technical_order.pdftext import ocr_available, page_layout_texts

#: Producers that indicate a born-digital AF TO toolchain.
_BORN_DIGITAL_PRODUCERS = re.compile(
    r"XPP|eBuild|Distiller|FrameMaker|XyEnterprise", re.I
)

#: Max page count for the full-document OCR fallback (see extract.py). Above
#: this, tesseract over every page costs minutes for documents that are
#: almost always large RPSTL/TM formats the column parser can't read
#: regardless of text source — triage reports OCR as impractical rather than
#: "available" once a document crosses this size, and the extractor gates the
#: fallback on the same threshold so the two never drift apart.
OCR_PAGE_BUDGET = 180

_MPL_HEADER = re.compile(r"FIGURE\s*&", re.I)
_MPL_CAGE_COLUMN = re.compile(r"\bCAGE\b")
_MPL_INDENTURE_RULER = re.compile(r"1\s?2\s?3\s?4\s?5\s?6\s?7")
_RPSTL_HEADER = re.compile(
    r"\bSMR\b.{0,40}\bCODE\b|\(UOC\)|USABLE ON CODE \(UOC\)", re.I
)
_RPSTL_SERVICES = re.compile(r"\bARMY\b.{0,30}\b(AIR|FORCE)\b", re.S)
_CABLE_HEADER = re.compile(r"\bREF DES\b.{0,200}\bFROM\b.{0,80}\bTO\b", re.S)

_SUPPLEMENT = re.compile(
    r"^\s*SUPPLEMENT\s*$|This (publication|manual) supplements", re.I | re.M
)
_MERGED = re.compile(r"BASIC AND ALL CHANGES\s+HAVE BEEN MERGED", re.I)
_CHANGE_LEVEL = re.compile(r"CHANGE\s+(\d+)\s*[-\u2013\u2014]", re.I)
_IPB_TITLE = re.compile(r"ILLUSTRATED PARTS BREAKDOWN|PARTS BREAKDOWN|PARTS LIST", re.I)
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
    # Median body-text density across the SAMPLED pages actually used for the
    # extraction_class verdict (excludes title/front-matter pages) — narrower
    # and more diagnostic than medianCharsPerPage, which includes them.
    body_median_chars_per_page: int = 0
    sampled_page_count: int = 0
    # Whether the OCR fallback in extract.py can actually run for this
    # document: tesseract present on PATH AND page_count within budget. False
    # for a born-digital document too (OCR is never attempted there) — this
    # field answers "would OCR help/run", not "is OCR needed".
    ocr_ready: bool = False
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
            "bodyMedianCharsPerPage": self.body_median_chars_per_page,
            "sampledPageCount": self.sampled_page_count,
            "ocrReady": self.ocr_ready,
            "signals": self.signals,
        }


def _sample_page_numbers(page_count: int) -> list[int]:
    """Title pages plus a spread of body-page fractions.

    Front matter (title/LOEP) is short relative to the body, so distinct BODY
    samples matter more than raw sample count — sampling six fractions rather
    than three keeps that true even for a short document, where coarser
    fractions collide onto the same one or two pages. ``page_count`` itself
    is always included: parts lists / drawing sets often run to the very end
    of the document, and the fraction sweep alone can miss the last page
    entirely on small documents due to rounding.
    """
    sample = {1, 2, 3, page_count}
    for frac in (0.25, 0.4, 0.5, 0.6, 0.75, 0.9):
        sample.add(max(1, min(page_count, round(page_count * frac))))
    return sorted(p for p in sample if 1 <= p <= page_count)


def triage_pdf(pdf_path: Path) -> TriageResult:  # noqa: C901 - linear signal-gathering pass; splitting hurts clarity
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
    page_samples: list[tuple[int, str]] = []
    for p in sample_pages:
        try:
            texts = page_layout_texts(pdf_path, first=p, last=p)
            page_samples.append((p, texts[0] if texts else ""))
        except Exception:
            page_samples.append((p, ""))
    sampled = [text for _, text in page_samples]
    char_counts = [len(re.sub(r"\s", "", t)) for t in sampled]
    median_chars = int(statistics.median(char_counts)) if char_counts else 0
    # Front matter (title/LOEP) pages skew short; exclude them by PAGE NUMBER
    # rather than sample position so the body estimate stays correct no
    # matter how the fraction sweep in _sample_page_numbers orders or dedupes
    # its picks. Falls back to every sample when the document is too short
    # to have any page past the front matter.
    body_counts = [
        len(re.sub(r"\s", "", text)) for page_no, text in page_samples if page_no > 3
    ] or char_counts
    body_median = int(statistics.median(body_counts)) if body_counts else 0

    born_digital_producer = bool(
        _BORN_DIGITAL_PRODUCERS.search(producer)
        or _BORN_DIGITAL_PRODUCERS.search(creator)
    )
    full_text = ""
    if body_median < 40:
        # Figure-heavy TOs (IPB plates, drawing sets) can put every SAMPLED body
        # page on a caption-only figure plate while the parts/text sections carry
        # a perfectly good text layer — confirm "no text" against the whole
        # document before writing the text layer off. Only runs on the rare
        # sample-says-blank path, so the ~5 ms/page pdftotext sweep stays off
        # the common triage path.
        text_rich_pages = 0
        try:
            all_texts = page_layout_texts(pdf_path)
            text_rich_pages = sum(
                1 for t in all_texts if len(re.sub(r"\s", "", t)) >= 200
            )
            full_text = "\n".join(all_texts)
        except Exception:
            pass
        if text_rich_pages >= 3:
            signals.append(
                f"sampled body pages are figure plates (median={body_median}) but "
                f"{text_rich_pages} pages document-wide are text-rich"
            )
            if born_digital_producer:
                extraction_class = "born-digital"
                signals.append(f"producer={producer or creator}")
            else:
                extraction_class = "scanned-dirty-ocr"
                signals.append(
                    "text present but producer not a known born-digital toolchain"
                )
        else:
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

    # When the samples landed on figure plates the full text (already extracted
    # for the text-rich check) carries the table headers the samples missed.
    text = full_text or "\n".join(sampled)
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

    # OCR readiness: report whether the extractor's OCR fallback can actually
    # run, not just whether the document looks like it needs it — a scanned
    # document with no OCR binary on PATH, or one too long for the fallback's
    # page budget, should say so up front rather than silently degrading with
    # only a generic "layout parser may be unreliable" warning downstream.
    tesseract_present = ocr_available()
    ocr_ready = (
        extraction_class != "born-digital"
        and tesseract_present
        and page_count <= OCR_PAGE_BUDGET
    )
    if extraction_class != "born-digital":
        if not tesseract_present:
            signals.append("OCR fallback unavailable: tesseract not on PATH")
        elif page_count > OCR_PAGE_BUDGET:
            signals.append(
                f"OCR fallback impractical: page_count={page_count} exceeds "
                f"budget={OCR_PAGE_BUDGET}"
            )

    return TriageResult(
        extraction_class=extraction_class,
        format_family=format_family,
        document_kind=document_kind,
        document_type=document_type,
        page_count=page_count,
        producer=producer,
        creator=creator,
        median_chars_per_page=median_chars,
        body_median_chars_per_page=body_median,
        sampled_page_count=len(sample_pages),
        ocr_ready=ocr_ready,
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
