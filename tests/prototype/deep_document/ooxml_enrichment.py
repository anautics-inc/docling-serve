"""PPTX-only enrichment of the Docling spine.

The Docling `DoclingDocument` is the structural truth (units, blocks, tables,
pictures). This module attaches the four things Docling's PPTX backend
documents that it does NOT extract:

  - theme        (deck-level — colour scheme + font scheme)
  - speaker notes (per slide)
  - background    (per slide — solid/gradient/image, slide→layout→master)
  - typography    (per text block — added in a later loop iteration)

Page-level enrichment (notes, background) joins by **slide order**: Docling
processes PPTX slides in presentation order, so spine `unit.index == i`
maps to the i-th OOXML slide part. That positional join needs no bbox/text
matching and is exact for PPTX.

This module is the ONLY place that touches OOXML directly. If a future format
(PDF) has no OOXML, the spine still stands on its own and this module is
simply skipped.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from .background import slide_background
from .ooxml import (
    MediaAsset,
    clean_notes,
    extract_text,
    normalize_part_target,
    parse_pptx,
    presentation_slide_parts,
    read_xml,
    rels_for,
)
from .theme import parse_theme

# A unit with no matching slide gets these inert defaults — never a crash.
_EMPTY_NOTES = {"raw": "", "cleaned": None, "junkFiltered": False}
_EMPTY_BACKGROUND = {"kind": "none", "color": None, "assetId": None, "source": "master"}


def _notes_for_slide(
    zf: zipfile.ZipFile, slide_part: str, slide_rels: dict[str, dict[str, str]], page_number: int
) -> dict[str, Any]:
    notes_raw = ""
    for rel in slide_rels.values():
        if rel["type"].endswith("/notesSlide"):
            target = normalize_part_target(slide_part, rel["target"])
            notes_raw = extract_text(read_xml(zf, target))
            break
    cleaned, junk_filtered = clean_notes(notes_raw, page_number)
    return {"raw": notes_raw, "cleaned": cleaned, "junkFiltered": junk_filtered}


def enrich_units(
    units: list[dict[str, Any]], pptx_path: str | Path
) -> tuple[dict[str, Any], dict[str, MediaAsset]]:
    """Attach theme / speakerNotes / background / sourcePart to spine units.

    Mutates `units` in place. Returns `(theme, background_assets)` where
    `background_assets` is any image binaries pulled in as slide backgrounds,
    deduped by sha256 and ready to merge into the manifest asset table.
    """
    path = Path(pptx_path)
    background_assets: dict[str, MediaAsset] = {}
    with zipfile.ZipFile(path) as zf:
        theme = parse_theme(zf)
        slide_parts = presentation_slide_parts(zf)
        for unit in units:
            index = unit["index"]
            if index >= len(slide_parts):
                # Docling reported more pages than OOXML has slides — record
                # inert defaults rather than guessing.
                unit["speakerNotes"] = dict(_EMPTY_NOTES)
                unit["background"] = dict(_EMPTY_BACKGROUND)
                unit["sourcePart"] = None
                continue
            slide_part = slide_parts[index]
            slide_rels = rels_for(zf, slide_part)
            unit["sourcePart"] = slide_part
            unit["speakerNotes"] = _notes_for_slide(
                zf, slide_part, slide_rels, unit["pageNumber"]
            )
            unit["background"] = slide_background(
                zf, slide_part, slide_rels, theme, background_assets
            )
    return theme, background_assets


def _normalize_text(text: str) -> str:
    """Whitespace-collapsed, lowercased key for matching Docling text to OOXML.

    Docling and OOXML read the same PPTX, so their text content lines up after
    normalization. Docling PPTX bboxes are shape-level (many list items share
    one bbox), which rules out a geometric join — text content is the key.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _substring_match(
    block_norm: str,
    para_index: dict[str, list[dict[str, Any]]],
    min_ratio: float,
) -> dict[str, Any] | None:
    """Fallback for near-misses (e.g. a title that is a prefix of a longer
    OOXML run, or vice versa). Accepts a match only when the shorter string is
    at least `min_ratio` of the longer — guards against short-token false hits.
    """
    best_key: str | None = None
    best_len = 0
    for key, queue in para_index.items():
        if not queue:
            continue
        shorter, longer = sorted((block_norm, key), key=len)
        if not shorter or shorter not in longer:
            continue
        if len(shorter) / len(longer) < min_ratio:
            continue
        if len(shorter) > best_len:
            best_len = len(shorter)
            best_key = key
    if best_key is None:
        return None
    return para_index[best_key].pop(0)


def attach_typography(
    units: list[dict[str, Any]],
    pptx_path: str | Path,
    *,
    min_substring_ratio: float = 0.6,
) -> dict[str, Any]:
    """Attach run-level typography to each Docling text block.

    Reuses the experiment5 OOXML typography machinery (`parse_pptx`) for
    fully inheritance-resolved paragraphs, then joins each Docling text block
    by content:

      1. Decoration exclusion — a block whose text equals an OOXML decoration
         (slide number / footer / date) is not a content miss; OOXML routes
         those out of the paragraph stream by design.
      2. Line-split exact match — Docling `text`-label items can merge what
         OOXML keeps as separate paragraphs; each line is matched on its own
         and the block collects every paragraph it hit.
      3. Substring fallback — `min_substring_ratio` guards near-misses.

    `min_substring_ratio` is a parameter, not a literal — tune per corpus.
    Mutates `units`; returns coverage counts.
    """
    path = Path(pptx_path)
    with zipfile.ZipFile(path) as zf:
        theme = parse_theme(zf)
    ooxml_units, _assets, _stats = parse_pptx(path, theme=theme)

    by_slide: dict[int, dict[str, list[dict[str, Any]]]] = {}
    decorations_by_slide: dict[int, set[str]] = {}
    for ooxml_unit in ooxml_units:
        para_index: dict[str, list[dict[str, Any]]] = {}
        for block in ooxml_unit.get("blocks", []):
            for paragraph in block.get("paragraphs", []) or []:
                para_text = "".join(run.get("text", "") for run in paragraph.get("runs", []))
                key = _normalize_text(para_text)
                if key:
                    para_index.setdefault(key, []).append(paragraph)
        by_slide[ooxml_unit["index"]] = para_index
        decorations_by_slide[ooxml_unit["index"]] = {
            _normalize_text(value)
            for value in (ooxml_unit.get("decorations") or {}).values()
            if _normalize_text(value)
        }

    matched = 0
    unmatched = 0
    decoration_skipped = 0
    # AUDIT F5: keep a capped sample of unmatched blocks (fixture-level
    # diagnostics) so the join can be improved without guessing which blocks
    # failed. Capped to keep the manifest bounded on large decks.
    unmatched_samples: list[dict[str, Any]] = []
    unmatched_sample_cap = 25
    for unit in units:
        para_index = by_slide.get(unit["index"], {})
        decorations = decorations_by_slide.get(unit["index"], set())
        for block in unit["blocks"]:
            if block["kind"] != "text" or not block.get("text"):
                continue
            block_norm = _normalize_text(block["text"])
            if block_norm and block_norm in decorations:
                block["paragraphs"] = []
                block["typographySource"] = "decoration"
                decoration_skipped += 1
                continue

            hits: list[dict[str, Any]] = []
            for raw_line in block["text"].split("\n"):
                line = _normalize_text(raw_line)
                if not line:
                    continue
                queue = para_index.get(line)
                if queue:
                    hits.append(queue.pop(0))
            if not hits and block_norm:
                fallback = _substring_match(block_norm, para_index, min_substring_ratio)
                if fallback is not None:
                    hits.append(fallback)

            if hits:
                block["paragraphs"] = hits
                block["typographySource"] = "matched"
                matched += 1
            else:
                block["paragraphs"] = []
                block["typographySource"] = "unmatched"
                unmatched += 1
                if len(unmatched_samples) < unmatched_sample_cap:
                    unmatched_samples.append(
                        {
                            "unitId": unit["unitId"],
                            "blockId": block["blockId"],
                            "textExcerpt": (block.get("text") or "")[:120],
                        }
                    )

    total = matched + unmatched
    return {
        "textBlocks": total,
        "typographyMatched": matched,
        "typographyUnmatched": unmatched,
        "decorationSkipped": decoration_skipped,
        "matchRate": round(matched / total, 4) if total else 0.0,
        "minSubstringRatio": min_substring_ratio,
        "unmatchedSamples": unmatched_samples,
    }


def enrichment_summary(units: list[dict[str, Any]]) -> dict[str, int]:
    """Coverage counts — how much enrichment actually attached."""
    return {
        "units": len(units),
        "unitsWithNotes": sum(1 for u in units if u.get("speakerNotes", {}).get("cleaned")),
        "unitsWithExplicitBackground": sum(
            1 for u in units if u.get("background", {}).get("source") == "slide"
        ),
        "unitsWithSourcePart": sum(1 for u in units if u.get("sourcePart")),
    }
