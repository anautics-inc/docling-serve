"""Maintenance Parts List (MPL) parser: verbatim rows + dot-indenture tree.

Operates on layout-preserved page text. Column boundaries are detected from
each parts-list header block (FIGURE & INDEX / PART NUMBER / CAGE /
DESCRIPTION / UNITS PER ASSY / USABLE ON CODE / SMR CODE), then every data
line is sliced into verbatim cells.

The BOM hierarchy is encoded by leading dots in the description column — one
dot per level below the end item:

    DEGREASER, Portable             level 0 (end item)
    . BASKET, Dipping-draining      level 1
    . . SCREW, Machine              level 2

A single-pass stack resolves each entry's parent: an entry at level *n*
parents to the most recent entry at level *n-1*. Exceptions:

- ``(AP)`` / ``(ATTACHING PARTS)`` rows are hardware joining the *preceding
  item at the same level* to its parent (``rowType=attaching-part``).
- ``REF`` units mean the item is listed under its true NHA elsewhere; the row
  is evidence, not a new child (``rowType=ref``).
- Kits (`KIT,` nouns) carry ``rowType=kit``; ``NO NUMBER`` part cells carry
  ``rowType=no-number``.

Everything as printed is preserved verbatim in ``*_raw`` fields, including
the indenture dots inside ``description_raw``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Figure numbers are either plain ("Figure 6.") or chapter-dash
# ("Figure 6-2."); sheet suffixes may omit the total ("(Sheet 2)").
_FIGURE_CAPTION = re.compile(
    r"^Figure\s+(\d+(?:-\d+)?[A-Z]?)\.\s+(.+?)"
    r"(?:\s*\(Sheet\s+(\d+)(?:\s+of\s+(\d+))?\))?\s*$"
)
# Case-insensitive: legacy headers ("Figure / and / index", "Part number",
# "FSCM") print in mixed case, and OCR output follows the print.
_HEADER_FIGURE = re.compile(r"\bFIG(?:URE)?\s*&|\bFIGURE\b", re.I)
_HEADER_RULER = re.compile(r"1\s?2\s?3\s?4\s?5\s?6\s?7")
_AP_GROUP_MARKER = re.compile(r"^\(?\s*ATTACHING PARTS\s*\)?$", re.I)
_AP_SEPARATOR = re.compile(r"^-{2,}\s*\*\s*-{2,}$")
_AP_SUFFIX = re.compile(r"\(AP\)\s*\.?\s*$")
_LEADING_DOTS = re.compile(r"^((?:\.\s*)+)")
_CAGE_WITH_SPILLOVER_DOTS = re.compile(r"^([0-9A-Z]{5})\s*((?:\.\s*)+)$")
_NSN_IN_DESC = re.compile(r"NSN[ :]*([0-9]{4}-?\s?[0-9]{2}-?\s?[0-9]{3}-?\s?[0-9]{4})")
_CAGE_IN_DESC = re.compile(r"\(([0-9A-Z]{5})\)")
_TERMINATOR = re.compile(
    r"^\s*(SECTION\s+[IVX0-9]+|CHAPTER\s+\d+|NUMERICAL INDEX|REFERENCE DESIGNATION|NSN INDEX)",
    re.I,
)
_PAGE_FOOTER = re.compile(r"^\s*(\d{1,4}|[0-9]+-[0-9]+|T\.?O\.?\s+\S+.*)\s*$")
# NOTE: single quantifier per atom — the previous `(\s*\.\s*){3,}$` form had
# adjacent ambiguous quantifiers and went exponential on long leader runs
# (one page cost 30+ seconds in re.sub).
_TRAILING_LEADERS = re.compile(r"(?:\.[ \t]*){3,}$")


@dataclass(slots=True)
class FigureRecord:
    figure_number: str
    figure_title: str = ""
    sheet_number: str = ""
    sheet_total: int | None = None
    page_number: int = 0
    media_key: str = ""
    # Clickable callout positions detected on the rendered sheet
    # (figure_hotspots.detect_figure_hotspots dicts).
    hotspots: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "figureNumber": self.figure_number,
            "figureTitle": self.figure_title,
            "sheetNumber": self.sheet_number,
            "sheetTotal": self.sheet_total,
            "pageNumber": self.page_number,
            "mediaKey": self.media_key,
            "hotspots": self.hotspots,
        }


@dataclass(slots=True)
class PartsListEntry:
    sequence: int
    page_number: int
    figure_number_raw: str = ""
    figure_index_raw: str = ""
    part_number_raw: str = ""
    cage_raw: str = ""
    description_raw: str = ""
    units_per_assembly_raw: str = ""
    usable_on_code_raw: str = ""
    smr_raw: str = ""
    nsn_raw: str = ""
    indenture_level: int = 0
    parent_sequence: int | None = None
    row_type: str = "part"
    kit_part_number: str = ""
    nomenclature: str = ""
    review_status: str = "auto-accepted"
    validation_flags: list[str] = field(default_factory=list)
    # Normalized [x0, y0, x1, y1] of the printed row on its page (fractions
    # of page size) — set by rowbox.attach_row_boxes for text-layer sources.
    row_box: tuple[float, float, float, float] | None = None

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "pageNumber": self.page_number,
            "rowBox": list(self.row_box) if self.row_box else None,
            "figureNumberRaw": self.figure_number_raw,
            "figureIndexRaw": self.figure_index_raw,
            "partNumberRaw": self.part_number_raw,
            "cageRaw": self.cage_raw,
            "descriptionRaw": self.description_raw,
            "unitsPerAssemblyRaw": self.units_per_assembly_raw,
            "usableOnCodeRaw": self.usable_on_code_raw,
            "smrRaw": self.smr_raw,
            "nsnRaw": self.nsn_raw,
            "indentureLevel": self.indenture_level,
            "parentSequence": self.parent_sequence,
            "rowType": self.row_type,
            "kitPartNumber": self.kit_part_number,
            "nomenclature": self.nomenclature,
            "reviewStatus": self.review_status,
            "validationFlags": self.validation_flags,
        }


@dataclass(slots=True)
class _Columns:
    """Character offsets of column starts on this page's table."""

    part: int
    cage: int | None
    desc: int
    units: int
    uoc: int | None
    smr: int | None

    def slice(self, line: str) -> dict[str, str]:
        def cell(start: int | None, end: int | None) -> str:
            if start is None:
                return ""
            return line[start:end].strip() if end is not None else line[start:].strip()

        bounds: list[tuple[str, int | None, int | None]] = []
        cage_or_desc = self.cage if self.cage is not None else self.desc
        bounds.append(("index", 0, self.part))
        bounds.append(("part", self.part, cage_or_desc))
        if self.cage is not None:
            bounds.append(("cage", self.cage, self.desc))
        bounds.append(("desc", self.desc, self.units))
        next_after_units = self.uoc if self.uoc is not None else self.smr
        bounds.append(("units", self.units, next_after_units))
        if self.uoc is not None:
            bounds.append(("uoc", self.uoc, self.smr))
        bounds.append(("smr", self.smr, None))
        out = {name: cell(s, e) for name, s, e in bounds}
        out.setdefault("cage", "")
        out.setdefault("uoc", "")
        out.setdefault("smr", "")
        return out


def _find_header(lines: list[str], start: int) -> tuple[_Columns, int] | None:
    """Locate a parts-list header block at/after ``start``; return columns and
    the line index just past the header."""
    for i in range(start, len(lines)):
        if not _HEADER_FIGURE.search(lines[i]):
            continue
        # A figure CAPTION ("Figure 6-1. Title") also contains the word
        # Figure — it is never the header row.
        if _FIGURE_CAPTION.search(lines[i].strip()):
            continue
        window = lines[i : i + 4]
        blob = "\n".join(window)
        if "DESCRIPTION" not in blob.upper() and not _HEADER_RULER.search(blob):
            continue

        def pos(pattern: str) -> int | None:
            best: int | None = None
            for w in window:
                m = re.search(pattern, w, re.I)
                if m and (best is None or m.start() < best):
                    best = m.start()
            return best

        part = pos(r"\bPART\b")
        number = pos(r"\bNUMBER\b")
        # FSCM is the pre-1990s name for the CAGE code column.
        cage = pos(r"\bCAGE\b|\bFSCM\b")
        ruler = pos(r"1\s?2\s?3\s?4\s?5\s?6\s?7")
        desc = pos(r"\bDESCRIPTION\b")
        units = pos(r"\bUNITS\b")
        assy = pos(r"\bASSY\b")
        uoc = pos(r"\bUSABLE\b")
        smr = pos(r"\bSMR\b")

        part_start = min(x for x in (part, number) if x is not None) if (part or number) else None
        desc_start = ruler if ruler is not None else desc
        units_start = min(x for x in (units, assy) if x is not None) if (units or assy) else None
        if part_start is None or desc_start is None or units_start is None:
            continue
        end = i + len(window)
        for j in range(i, min(i + 4, len(lines))):
            if re.search(r"SHEET NO\.|CODE\s*$|1\s?2\s?3\s?4\s?5\s?6\s?7", lines[j], re.I):
                end = j + 1
        body_sample = [
            ln for ln in lines[end : end + 30] if ln.strip() and not _TERMINATOR.match(ln)
        ]
        cols = _Columns(
            part=_snap_boundary(body_sample, part_start),
            cage=_snap_boundary(body_sample, cage) if cage is not None else None,
            desc=_snap_boundary(body_sample, desc_start),
            units=_snap_boundary(body_sample, units_start),
            uoc=_snap_boundary(body_sample, uoc) if uoc is not None else None,
            smr=_snap_boundary(body_sample, smr) if smr is not None else None,
        )
        return cols, end
    return None


def _normalize_cage_and_description(cage_raw: str, desc_raw: str) -> tuple[str, str]:
    """Recover indenture dots that spill into the CAGE column on continuation pages.

    When the CAGE value is short, the first description dot can land in the CAGE
    cell (e.g. ``80205   .`` + ``. NUT`` → ``. . NUT``). Merge spillover dots
    back into ``description_raw`` and return a clean five-character CAGE code.
    """
    cage = cage_raw.strip()
    desc = desc_raw
    spill = ""
    if m := _CAGE_WITH_SPILLOVER_DOTS.match(cage):
        cage = m.group(1)
        spill = m.group(2)
    if not spill:
        return cage, desc

    spill_levels = spill.count(".")
    desc_levels = 0
    if m := _LEADING_DOTS.match(desc):
        desc_levels = m.group(1).count(".")
        desc = _LEADING_DOTS.sub("", desc).lstrip()
    merged_levels = spill_levels + desc_levels
    if merged_levels:
        desc = f"{('. ' * merged_levels).rstrip()} {desc}".strip()
    return cage, desc


def _indenture_level(description: str) -> int:
    if m := _LEADING_DOTS.match(description):
        return m.group(1).count(".")
    return 0


def _snap_boundary(sample: list[str], anchor: int) -> int:
    """Snap a header-derived column anchor onto a real whitespace gutter.

    Headers are centered over their data, so data often starts left of the
    header text. A valid boundary is a character column that is whitespace in
    every sampled data line; search near the anchor, preferring the rightmost
    valid column at or just past it.
    """
    if not sample:
        return max(0, anchor - 2)

    def is_gutter(c: int) -> bool:
        if c <= 0:
            return False
        for ln in sample:
            if c - 1 < len(ln) and ln[c - 1] != " ":
                return False
        return True

    for c in range(anchor + 2, max(anchor - 12, 0), -1):
        if is_gutter(c):
            return c
    return max(0, anchor - 2)


def parse_parts_lists(
    pages: list[str], *, first_page_number: int = 1
) -> tuple[list[PartsListEntry], list[FigureRecord]]:
    """Parse every parts-list table across ``pages``.

    Returns (entries, figures). Page numbers are PDF 1-based.
    """
    entries: list[PartsListEntry] = []
    figures: list[FigureRecord] = []
    seen_figures: set[tuple[str, str]] = set()
    seq = 0
    stack: dict[int, int] = {}  # indenture level -> sequence of latest entry
    last_at_level: dict[int, int] = {}
    current_figure = ""
    expect_figure_token = False
    ap_active = False
    ap_owner: int | None = None
    ap_level: int | None = None

    for page_offset, page in enumerate(pages):
        page_number = first_page_number + page_offset
        lines = page.splitlines()

        for ln in lines:
            if m := _FIGURE_CAPTION.search(ln.strip()):
                title = _TRAILING_LEADERS.sub("", m.group(2)).strip().rstrip(".")
                if 2 < len(title) < 90 and not title.endswith(","):
                    key = (m.group(1), m.group(3) or "")
                    if key not in seen_figures:
                        seen_figures.add(key)
                        figures.append(
                            FigureRecord(
                                figure_number=m.group(1),
                                figure_title=title,
                                sheet_number=m.group(3) or "",
                                sheet_total=int(m.group(4)) if m.group(4) else None,
                                page_number=page_number,
                            )
                        )

        cursor = 0
        while True:
            found = _find_header(lines, cursor)
            if not found:
                break
            cols, body_start = found
            cursor = body_start
            expect_figure_token = True

            i = body_start
            while i < len(lines):
                raw_line = lines[i].rstrip("\n")
                stripped = raw_line.strip()
                i += 1
                if not stripped:
                    continue
                if _TERMINATOR.match(stripped):
                    cursor = i
                    break
                if _HEADER_FIGURE.search(raw_line) and _find_header(lines, i - 1):
                    cursor = i - 1
                    break
                if m := _FIGURE_CAPTION.search(stripped):
                    current_figure = m.group(1)
                    continue
                if _AP_SEPARATOR.match(stripped):
                    ap_active = False
                    continue

                cells = cols.slice(raw_line.ljust(cols.units + 40))
                cage_cell, desc = _normalize_cage_and_description(
                    cells["cage"], cells["desc"]
                )
                if _AP_GROUP_MARKER.match(desc) or _AP_GROUP_MARKER.match(stripped):
                    ap_active = True
                    ap_owner = entries[-1].sequence if entries else None
                    ap_level = entries[-1].indenture_level if entries else None
                    continue

                has_payload = any(
                    cells[c] for c in ("part", "cage", "units", "uoc", "smr")
                )
                if not desc and not has_payload and not cells["index"]:
                    continue
                # Wrapped description text: no payload cells, continue previous.
                if not has_payload and entries and desc and not _LEADING_DOTS.match(desc):
                    prev = entries[-1]
                    prev.description_raw = f"{prev.description_raw} {desc}".strip()
                    if m := _NSN_IN_DESC.search(prev.description_raw):
                        prev.nsn_raw = re.sub(r"\s", "", m.group(1))
                    continue
                if not desc and not cells["part"]:
                    continue
                # Page-footer noise that slipped through column slicing.
                if not has_payload and _PAGE_FOOTER.match(stripped) and len(stripped) < 30:
                    continue

                seq += 1
                level = _indenture_level(desc)

                index_cell = cells["index"]
                if m := re.match(r"^(\d+(?:-\d+)?[A-Z]?)\s*-\s*$", index_cell):
                    # "1-" / "6-1-" form: figure number preceding the first index.
                    current_figure = m.group(1)
                    figure_index = ""
                elif m := re.match(r"^(\d+(?:-\d+)?[A-Z]?)-?\s+(\S.*)$", index_cell):
                    # "1   6" form: figure number + index (page continuation).
                    current_figure = m.group(1)
                    figure_index = m.group(2).strip()
                elif expect_figure_token and re.match(r"^\d+(?:-\d+)?[A-Z]?$", index_cell):
                    # First row after a header: a bare number is the figure.
                    current_figure = index_cell
                    figure_index = ""
                elif m := re.match(r"^-\s*(\S.*)$", index_cell):
                    # Legacy "-1", "-2" continuation: index under the current
                    # figure, dash printed as a leader.
                    figure_index = m.group(1).strip()
                else:
                    figure_index = index_cell
                expect_figure_token = False

                row_type = "part"
                if level == 0:
                    row_type = "end-item"
                units = cells["units"]
                if units.upper() == "REF":
                    row_type = "ref"
                if re.match(r"^KIT[, ]", desc.lstrip(". ").upper()):
                    row_type = "kit"
                part_cell = cells["part"]
                if re.match(r"^NO\s+NUMBER$", part_cell, re.I):
                    row_type = "no-number"
                is_ap = bool(_AP_SUFFIX.search(desc))
                if ap_active and ap_level is not None and level == ap_level:
                    is_ap = True
                elif ap_active and ap_level is not None and level != ap_level:
                    ap_active = False
                if is_ap and row_type == "part":
                    row_type = "attaching-part"

                if is_ap and ap_active and ap_owner is not None:
                    parent: int | None = ap_owner
                elif is_ap and not ap_active:
                    # (AP)-suffixed row: attaches the preceding same-level item.
                    parent = last_at_level.get(level)
                else:
                    parent = stack.get(level - 1) if level > 0 else None

                entry = PartsListEntry(
                    sequence=seq,
                    page_number=page_number,
                    figure_number_raw=current_figure,
                    figure_index_raw=figure_index,
                    part_number_raw=part_cell,
                    cage_raw=cage_cell,
                    description_raw=desc,
                    units_per_assembly_raw=units,
                    usable_on_code_raw=cells["uoc"],
                    smr_raw=cells["smr"],
                    indenture_level=level,
                    parent_sequence=parent,
                    row_type=row_type,
                )
                if m := _NSN_IN_DESC.search(desc):
                    entry.nsn_raw = re.sub(r"\s", "", m.group(1))
                if not entry.cage_raw and (m := _CAGE_IN_DESC.search(desc)):
                    entry.cage_raw = m.group(1)
                noun = _LEADING_DOTS.sub("", desc)
                noun = _TRAILING_LEADERS.sub("", noun).strip(" .")
                entry.nomenclature = noun.split(" (")[0].strip()

                if not entry.part_number_raw and entry.row_type in ("part", "end-item"):
                    entry.review_status = "needs-review"
                    entry.validation_flags.append("missing part number")
                if entry.row_type not in ("end-item",) and not entry.description_raw:
                    entry.review_status = "needs-review"
                    entry.validation_flags.append("empty description")

                if not is_ap:
                    stack[level] = seq
                    for deeper in [k for k in stack if k > level]:
                        del stack[deeper]
                last_at_level[level] = seq
                entries.append(entry)
            else:
                cursor = len(lines)

    return entries, figures
