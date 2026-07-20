"""Full-document structured content for a technical order.

The BOM pipeline reads title metadata, parts lists, and figures; this module
captures EVERYTHING ELSE so the whole manual is digitally redisplayable:
front matter (title page, TOC, list of illustrations/tables), chapter and
section headings, numbered prose paragraphs, WARNING/CAUTION/NOTE
admonitions, and foldout references. The result is ``captify.to.v2``:
a page-by-page list of typed blocks that a viewer can re-render as a living
document and a graph consumer can hydrate as paragraph-level entities.

Everything here keys off structure (numbering grammars, dot leaders, letter
case, indentation), never off document-specific strings.
"""

from __future__ import annotations

import re
from typing import Any

from docling_serve.technical_order.contract import (
    LEGACY_TO_SCHEMA_ID,
    TO_SCHEMA_ID,
    inherited_markings,
    provenance,
    source_geometry,
    stable_id,
)

# ---------------------------------------------------------------- grammars
# "1-1", "vii", "6-10/(6-11 blank)" style page references at the end of a
# dot-leader line.
_TOC_LINE = re.compile(
    r"^\s*(?P<num>\d[\d.\-]*|[A-Z]{1,3}-\d+)?\s+"
    r"(?P<title>[^.\s].*?)\s*"
    r"(?:\.\s*){3,}\s*"
    r"(?P<page>[ivxlcdm]+|[A-Z]?\d+(?:-\d+)?(?:/\([^)]*\))?)\s*$",
    re.I,
)
_TOC_HEADER = re.compile(
    r"TABLE OF CONTENTS|LIST OF ILLUSTRATIONS|LIST OF TABLES", re.I
)
_CHAPTER = re.compile(r"^\s*CHAPTER\s+(\d+)\s*$", re.I)
_SECTION = re.compile(r"^\s*SECTION\s+([IVXLC]+)\b\s*(.*)$")
# "1.1 DESCRIPTION." / "5.26.1 TEST AFTER OVERHAUL." — a numbered heading is
# short, ends the sentence immediately, and carries no lowercase prose.
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+([A-Z][A-Z0-9 ,/&()'-]+)\.?\s*$")
# "1.1.2 Controls and Instruments. All operating controls..." — a numbered
# paragraph continues with prose on the same line.
_NUMBERED_PARA = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(\S.*)$")
# Safety-summary style "1 GENERAL SAFETY INSTRUCTIONS."
_ADMONITION = re.compile(r"^\s*(WARNING|CAUTION|NOTE)S?\s*$", re.I)
# Centered ALL-CAPS banner lines (front-matter headings, chapter titles).
_CAPS_BANNER = re.compile(r"^[A-Z][A-Z0-9 ,/&()'-]{3,}$")
_TO_FOOTER = re.compile(r"^\s*T\.?O\.?\s+\S+\s*$", re.I)
_PAGE_FOOTER = re.compile(
    r"^\s*(?:[A-Z]{0,2}[\divxlc]+(?:-\d+)?(?:/\([^)]*blank\)?)?|Change\s+\d+.*)\s*$",
    re.I,
)
_FOLDOUT = re.compile(r"^\s*(FO-\d+)\.?\s+(\S.*?)\s*$")
_FIGURE_CAPTION = re.compile(
    r"^\s*Figure\s+(\d+(?:-\d+)?[A-Z]?)\.?\s*(.*?)(?:\s*\(Sheet\s+\d+(?:\s+of\s+\d+)?\))?\s*\.?\s*$"
)
_TABLE_CAPTION = re.compile(r"^\s*Table\s+(\d+(?:-\d+)?)\.?\s+(\S.*)$")
_HYPHEN_WRAP = re.compile(r"(\w)-$")

_FRONT_MATTER_KINDS = {"toc", "list-of-illustrations", "list-of-tables"}

# In-prose cross references. Links are GROUNDED: a match only becomes a link
# when its target exists in this same document (figure set, paragraph-number
# set, parts list), so shaped-alike tokens never fabricate references.
_FIG_REF = re.compile(
    r"\bfigures?\s+(\d+(?:-\d+)?[A-Z]?)"
    r"(?:\s*[,;]\s*(?:item\s+)?(\d{1,3}[A-Z]?))?",
    re.I,
)
_PARA_REF = re.compile(r"\bparagraphs?\s+(\d+(?:\.\d+)+)", re.I)
# Part-number-shaped tokens: spec prefixes (MS35338-52), vendor numbers with
# dashes (19300-3), or bare 4-6 digit vendor numbers (21435).
_PART_TOKEN = re.compile(r"\b([A-Z]{1,4}\d[\dA-Z]*(?:-[\dA-Z]+)+|\d{4,6}(?:-\d+)?)\b")
_PN_NORM = re.compile(r"[^A-Z0-9]")


def _normalize_pn(value: str) -> str:
    return _PN_NORM.sub("", value.upper())


def _detect_links(
    text: str,
    *,
    figure_numbers: set[str],
    paragraph_numbers: set[str],
    part_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Grounded cross-reference links with character spans into ``text``."""
    links: list[dict[str, Any]] = []
    for m in _FIG_REF.finditer(text):
        num = m.group(1)
        if num not in figure_numbers:
            continue
        link: dict[str, Any] = {
            "type": "figure",
            "target": num,
            "start": m.start(),
            "end": m.end(),
        }
        if m.group(2):
            link["callout"] = m.group(2)
        links.append(link)
    for m in _PARA_REF.finditer(text):
        if m.group(1) in paragraph_numbers:
            links.append(
                {
                    "type": "paragraph",
                    "target": m.group(1),
                    "start": m.start(),
                    "end": m.end(),
                }
            )
    covered = [(link["start"], link["end"]) for link in links]
    for m in _PART_TOKEN.finditer(text):
        part = part_index.get(_normalize_pn(m.group(1)))
        if part is None or any(s <= m.start() < e for s, e in covered):
            continue
        links.append(
            {
                "type": "part",
                "target": part["partNumber"],
                "sequence": part["sequence"],
                "start": m.start(),
                "end": m.end(),
            }
        )
    links.sort(key=lambda link: link["start"])
    return links


def _toc_kind(lines: list[str]) -> str:
    """Front-matter kind from the page's own banner line — a TOC page lists
    "LIST OF ILLUSTRATIONS" as an entry, so scanning the whole body misfiles it."""
    for ln in lines[:6]:
        upper = ln.strip().upper()
        if "TABLE OF CONTENTS" in upper:
            return "toc"
        if "LIST OF ILLUSTRATIONS" in upper:
            return "list-of-illustrations"
        if "LIST OF TABLES" in upper:
            return "list-of-tables"
    return "toc"


def _clean_lines(page_text: str) -> list[str]:
    """Page lines with running headers/footers dropped (kept for provenance
    by the caller if needed) but interior blank lines preserved."""
    lines = page_text.splitlines()
    # Trim leading/trailing blanks.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if _TO_FOOTER.match(s) and (i <= 1 or i >= len(lines) - 2):
            continue
        if _PAGE_FOOTER.match(s) and i >= len(lines) - 2 and s:
            continue
        out.append(ln)
    return out


def _flush_paragraph(blocks: list[dict], buf: list[str], number: str) -> None:
    if not buf:
        return
    text = ""
    for piece in buf:
        piece = piece.strip()
        if not piece:
            continue
        if _HYPHEN_WRAP.search(text):
            text = _HYPHEN_WRAP.sub(r"\1", text) + piece
        else:
            text = f"{text} {piece}".strip()
    if text:
        blocks.append({"type": "paragraph", "number": number, "text": text})


def _parse_prose_page(lines: list[str]) -> list[dict]:  # noqa: C901 - one linear scanner
    """Headings, numbered paragraphs, and admonitions from a prose page."""
    blocks: list[dict] = []
    para_buf: list[str] = []
    para_num = ""
    admonition: str | None = None
    admonition_buf: list[str] = []

    def flush_all() -> None:
        nonlocal para_num, admonition
        _flush_paragraph(blocks, para_buf, para_num)
        para_buf.clear()
        para_num = ""
        if admonition and admonition_buf:
            _flush_paragraph(blocks, admonition_buf, "")
            blocks[-1]["type"] = "admonition"
            blocks[-1]["kind"] = admonition.lower()
            del blocks[-1]["number"]
        admonition = None
        admonition_buf.clear()

    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            if admonition and admonition_buf:
                flush_all()
            elif para_buf:
                # Blank inside admonition body only separates; prose flushes.
                _flush_paragraph(blocks, para_buf, para_num)
                para_buf.clear()
                para_num = ""
            i += 1
            continue

        if admonition is not None:
            admonition_buf.append(s)
            i += 1
            continue

        if m := _ADMONITION.match(s):
            flush_all()
            admonition = m.group(1).upper()
            i += 1
            continue

        if m := _CHAPTER.match(s):
            flush_all()
            title = ""
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _CAPS_BANNER.match(lines[j].strip()):
                title = lines[j].strip()
                i = j
            blocks.append(
                {"type": "heading", "level": 1, "number": m.group(1), "text": title}
            )
            i += 1
            continue

        if m := _SECTION.match(s):
            flush_all()
            blocks.append(
                {
                    "type": "heading",
                    "level": 2,
                    "number": m.group(1),
                    "text": m.group(2).strip(),
                }
            )
            i += 1
            continue

        if m := _FOLDOUT.match(s):
            flush_all()
            blocks.append({"type": "foldout", "number": m.group(1), "text": m.group(2)})
            i += 1
            continue

        if m := _FIGURE_CAPTION.match(s):
            flush_all()
            blocks.append(
                {"type": "figure-caption", "number": m.group(1), "text": m.group(2)}
            )
            i += 1
            continue

        if m := _TABLE_CAPTION.match(s):
            flush_all()
            blocks.append(
                {"type": "table-caption", "number": m.group(1), "text": m.group(2)}
            )
            i += 1
            continue

        if m := _NUMBERED_HEADING.match(s):
            # Distinguish "1.1 DESCRIPTION." (heading) from a numbered ALL-CAPS
            # paragraph by length: headings stay short.
            if len(m.group(2)) <= 60:
                flush_all()
                level = 2 + m.group(1).count(".")
                blocks.append(
                    {
                        "type": "heading",
                        "level": level,
                        "number": m.group(1),
                        "text": m.group(2).strip(". "),
                    }
                )
                i += 1
                continue

        if m := _NUMBERED_PARA.match(s):
            flush_all()
            para_num = m.group(1)
            para_buf.append(m.group(2))
            i += 1
            continue

        if _CAPS_BANNER.match(s) and not para_buf:
            flush_all()
            blocks.append({"type": "heading", "level": 2, "number": "", "text": s})
            i += 1
            continue

        para_buf.append(s)
        i += 1

    flush_all()
    return blocks


def _parse_toc_page(lines: list[str]) -> list[dict]:
    blocks: list[dict] = []
    for ln in lines:
        s = ln.strip()
        if not s or _TOC_HEADER.search(s):
            continue
        if m := _TOC_LINE.match(s):
            title = m.group("title").strip()
            if not title or title.lower() in {"chapter", "number", "title", "page"}:
                continue
            blocks.append(
                {
                    "type": "toc-entry",
                    "number": (m.group("num") or "").strip(),
                    "text": title,
                    "page": m.group("page"),
                }
            )
    return blocks


def _apply_links(
    out_pages: list[dict[str, Any]],
    figure_numbers: set[str] | None,
    part_entries: list[dict[str, Any]] | None,
) -> int:
    """Cross-reference link pass — runs after the full parse so the
    paragraph-number set covers the whole document."""
    fig_set = {str(n) for n in (figure_numbers or set())}
    part_index: dict[str, dict[str, Any]] = {}
    for row in part_entries or []:
        pn = str(row.get("partNumber") or "")
        key = _normalize_pn(pn)
        if key and key not in part_index:
            part_index[key] = {
                "partNumber": pn,
                "sequence": int(row.get("sequence") or 0),
            }
    if not fig_set and not part_index:
        return 0
    para_numbers = {
        str(b.get("number"))
        for p in out_pages
        for b in p["blocks"]
        if b.get("number") and b["type"] in ("paragraph", "heading")
    }
    link_count = 0
    for p in out_pages:
        for b in p["blocks"]:
            if b["type"] not in ("paragraph", "admonition"):
                continue
            links = _detect_links(
                b.get("text") or "",
                figure_numbers=fig_set,
                paragraph_numbers=para_numbers,
                part_index=part_index,
            )
            if links:
                b["links"] = links
                link_count += len(links)
    return link_count


def parse_content(
    pages: list[str],
    *,
    figure_pages: dict[int, dict[str, Any]] | None = None,
    parts_pages: set[int] | None = None,
    figure_numbers: set[str] | None = None,
    part_entries: list[dict[str, Any]] | None = None,
    document_id: str = "",
    markings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured content for the whole document (1-based page numbers).

    ``figure_pages`` maps page number -> figure record info for pages already
    claimed by the figure pipeline; ``parts_pages`` is the set of pages the
    parts-list parser consumed. Those pages get reference blocks rather than
    re-parsed prose so every page is accounted for exactly once.

    ``figure_numbers`` and ``part_entries`` (``{partNumber, sequence}`` rows)
    ground the cross-reference link pass: prose mentions of figures,
    paragraphs, and part numbers gain ``links`` with character spans, but
    ONLY when the target exists in this document.
    """
    figure_pages = figure_pages or {}
    parts_pages = parts_pages or set()
    document_id = document_id or stable_id("technical-order", "content-only", *pages)
    out_pages: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for idx, text in enumerate(pages):
        page_no = idx + 1
        lines = _clean_lines(text or "")
        body = "\n".join(lines)
        blocks: list[dict]
        if page_no == 1:
            kind = "title-page"
            blocks = [
                {"type": "title-line", "text": ln.strip()} for ln in lines if ln.strip()
            ]
        elif fig := figure_pages.get(page_no):
            kind = "figure"
            blocks = [
                {
                    "type": "figure-ref",
                    "number": str(fig.get("figureNumber") or ""),
                    "text": str(fig.get("figureTitle") or ""),
                    "mediaKey": str(fig.get("mediaKey") or ""),
                    "figureSheetId": str(fig.get("id") or ""),
                }
            ]
        elif page_no in parts_pages:
            kind = "parts-list"
            blocks = [
                {
                    "type": "parts-ref",
                    "text": "Maintenance parts list (see BOM entries)",
                }
            ]
        elif not body.strip():
            kind = "blank"
            blocks = []
        elif any(_TOC_HEADER.search(ln) for ln in lines[:6]):
            kind = _toc_kind(lines)
            blocks = _parse_toc_page(lines)
        else:
            kind = "prose"
            blocks = _parse_prose_page(lines)

        page_id = stable_id("page", document_id, page_no)
        page_markings = inherited_markings(markings, document_id)
        for block_index, b in enumerate(blocks):
            b["id"] = stable_id(
                "content-block",
                document_id,
                page_no,
                block_index,
                b["type"],
                b.get("number", ""),
            )
            b["provenance"] = provenance(
                method="layout-text",
                parser="docling-serve.technical-order.content",
                version="2",
                confidence=1.0,
                geometry=source_geometry(page_no),
            )
            if page_markings:
                b["markings"] = inherited_markings(page_markings, page_id)
            counts[b["type"]] = counts.get(b["type"], 0) + 1
        page_value = {
            "id": page_id,
            "pageNumber": page_no,
            "kind": kind,
            "blocks": blocks,
            "provenance": provenance(
                method="layout-text",
                parser="docling-serve.technical-order.content",
                version="2",
                confidence=1.0,
                geometry=source_geometry(page_no),
            ),
        }
        if page_markings:
            page_value["markings"] = page_markings
        out_pages.append(page_value)

    link_count = _apply_links(out_pages, figure_numbers, part_entries)

    kinds: dict[str, int] = {}
    for p in out_pages:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    return {
        "id": stable_id("technical-order-content", document_id, len(pages)),
        "schema": TO_SCHEMA_ID,
        "compatibleSchemas": [LEGACY_TO_SCHEMA_ID],
        "documentId": document_id,
        "pageCount": len(pages),
        "pageKinds": kinds,
        "blockCounts": counts,
        "linkCount": link_count,
        "pages": out_pages,
    }
