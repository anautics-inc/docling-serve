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

from docling_serve.technical_order.contract import provenance, source_geometry
from docling_serve.technical_order.part_attributes import (
    classify_spec_family,
    parse_part_name,
)

# Figure numbers are either plain ("Figure 6.") or chapter-dash
# ("Figure 6-2."); sheet suffixes may omit the total ("(Sheet 2)").
_FIGURE_CAPTION = re.compile(
    r"^Figure\s+(\d+(?:-\d+)?[A-Z]?)\.\s+(.+?)"
    r"(?:\s*\(Sheet\s+(\d+)(?:\s+of\s+(\d+))?\))?\s*$"
)
# Bare captions ("Figure 3-1", "Figure 3-1.", "Figure 3-1 (Sheet 2 of 5)") —
# figure-only sheets (no parts table on the page) are often captioned with
# nothing but the number: no titled sentence, sometimes not even a period.
# Group indices line up with ``_FIGURE_CAPTION`` (title is always ``""``) so
# both patterns can be tried interchangeably by callers.
_FIGURE_CAPTION_BARE = re.compile(
    r"^Figure\s+(\d+(?:-\d+)?[A-Z]?)()\.?"
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
# Table-of-contents figure references look like captions but end in a leader
# followed by a printed page locator ("Figure 2. Housing .... 2-4"). They must
# never become renderable figure records; a real caption on the drawing sheet
# carries no trailing page locator.
_FIGURE_TOC_REFERENCE = re.compile(r"(?:\.[ \t]*){3,}\s*\d+(?:-\d+)?\s*$")
# A bare figure-number banner ("9-1-", "9-4-") that restates the figure ahead
# of its real end-item row and carries no part/units/SMR data of its own. Left
# unhandled, it either corrupts an unrelated entry (falls into the wrapped-
# continuation merge below, since it has no leading dot) or becomes a fake
# no-part-number row; it only ever exists to declare the figure switch.
_FIGURE_BANNER = re.compile(r"^(\d+(?:-\d+)?[A-Z]?)\s*-\s*$")
# The same banner without the trailing dash ("6-6" alone in the index column):
# some layouts print the figure number bare before the figure's first row.
_FIGURE_TOKEN = re.compile(r"^\d+(?:-\d+)?[A-Z]?$")
# Any lowercase word: distinguishes prose/TOC lines ("Figure 2-1. Cabinet
# assembly") from all-caps column-banner lines when validating tall headers.
_LOWERCASE_WORD = re.compile(r"[a-z]{2,}")
# A bare page number ("6-10", "42") — centered page footers land in the
# description column and must not merge into a wrapped description.
_BARE_PAGE_NUMBER = re.compile(r"^\d+(?:-\d+)?$")
# A date footer ("20 FEBRUARY 2015", change-date banners). The month tolerates
# one interior space — scanned pages OCR as "FEBRU ARY".
_DATE_FOOTER = re.compile(r"^\d{1,2}\s+[A-Z]{2,9} ?[A-Z]{0,7}\s+\d{4}\b")
# The per-assembly "USABLE ON CODE" legend that recaps which lettered code
# (A, B, ...) maps to which assembly/model, printed after a group of end-item
# rows as its own mini header ("CODES ... USABLE ON") followed by one
# "<letter>  <value>" row per code. Explanatory prose, never BOM row data —
# matched against the untouched printed line because column slicing can chop
# "CODES" across the CAGE/DESCRIPTION boundary on narrower tables.
_USABLE_ON_LEGEND_HEADER = re.compile(r"\bCODES?\b.{0,20}\bUSABLE\s+ON\b", re.I)
_USABLE_ON_LEGEND_ROW = re.compile(r"^[A-Z]\s{2,}\S.*$")
# Explanatory footnotes below a table ("NOTE * : SITE DEPENDENT") — prose.
_NOTE_LINE = re.compile(r"^NOTES?\s*\*{0,4}\s*[:\s]", re.I)


def _match_figure_caption(line: str) -> re.Match[str] | None:
    """Match a titled or bare figure caption; ``None`` if neither fits."""
    return _FIGURE_CAPTION.search(line) or _FIGURE_CAPTION_BARE.search(line)


@dataclass(slots=True)
class FigureRecord:
    figure_number: str
    figure_title: str = ""
    sheet_number: str = ""
    sheet_total: int | None = None
    page_number: int = 0
    media_key: str = ""
    # Vector "digital twin" of the sheet (pdftocairo SVG of the source page):
    # exact line-art tracing, only produced for born-digital sources.
    vector_key: str = ""
    # Clickable callout positions detected on the rendered sheet
    # (figure_hotspots.detect_figure_hotspots dicts).
    hotspots: list[dict] = field(default_factory=list)
    stable_id: str = ""
    markings: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        value = {
            "id": self.stable_id,
            "figureNumber": self.figure_number,
            "figureTitle": self.figure_title,
            "sheetNumber": self.sheet_number,
            "sheetTotal": self.sheet_total,
            "pageNumber": self.page_number,
            "mediaKey": self.media_key,
            "vectorKey": self.vector_key,
            "hotspots": self.hotspots,
            "provenance": provenance(
                method="layout-text",
                parser="docling-serve.technical-order.figure-caption",
                version="2",
                confidence=1.0,
                geometry=source_geometry(self.page_number),
            ),
        }
        if self.markings:
            value["markings"] = self.markings
        return value


@dataclass(slots=True)
class PartsListEntry:
    sequence: int
    page_number: int
    figure_number_raw: str = ""
    figure_index_raw: str = ""
    part_number_raw: str = ""
    reference_designator_raw: str = ""
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
    # Part -> callout link: where this part's index callout sits on its figure
    # sheet (normalized box) and which rendered figure image it lives on. Set by
    # the figure-hotspot pass so the UI can jump part -> illustration callout and
    # know where to re-stamp the callout when the part reference changes.
    callout_box: tuple[float, float, float, float] | None = None
    figure_media_key: str = ""
    stable_id: str = ""
    parent_id: str | None = None
    markings: dict = field(default_factory=dict)
    extraction_method: str = "layout-text"

    def as_dict(self) -> dict:
        method = (
            "vision-model" if self.review_status == "vision" else self.extraction_method
        )
        confidence = (
            0.5
            if self.review_status == "vision"
            else (0.5 if self.review_status == "needs-review" else 1.0)
        )
        value = {
            "id": self.stable_id,
            "sequence": self.sequence,
            "pageNumber": self.page_number,
            "rowBox": list(self.row_box) if self.row_box else None,
            "figureNumberRaw": self.figure_number_raw,
            "figureIndexRaw": self.figure_index_raw,
            "partNumberRaw": self.part_number_raw,
            "refDesRaw": self.reference_designator_raw,
            "cageRaw": self.cage_raw,
            "descriptionRaw": self.description_raw,
            "unitsPerAssemblyRaw": self.units_per_assembly_raw,
            "usableOnCodeRaw": self.usable_on_code_raw,
            "smrRaw": self.smr_raw,
            "nsnRaw": self.nsn_raw,
            "indentureLevel": self.indenture_level,
            "parentSequence": self.parent_sequence,
            "parentId": self.parent_id,
            "rowType": self.row_type,
            "kitPartNumber": self.kit_part_number,
            "nomenclature": self.nomenclature,
            # Mined name attributes (raws above are never overwritten).
            **parse_part_name(self.nomenclature or self.description_raw),
            "specFamily": classify_spec_family(self.part_number_raw),
            "reviewStatus": self.review_status,
            "validationFlags": self.validation_flags,
            "calloutBox": list(self.callout_box) if self.callout_box else None,
            "figureMediaKey": self.figure_media_key,
            "provenance": provenance(
                method=method,
                parser="docling-serve.technical-order.parts-list",
                version="2",
                confidence=confidence,
                geometry=source_geometry(self.page_number, self.row_box),
            ),
        }
        if self.markings:
            value["markings"] = self.markings
        return value


#: Cell names every consumer can rely on being present (blank when the table
#: doesn't print that column).
_CELL_NAMES = (
    "index",
    "refdes",
    "part",
    "cage",
    "indent",
    "desc",
    "units",
    "uoc",
    "smr",
    "nsn",
)


@dataclass(slots=True)
class _Columns:
    """Character offsets of column starts, in print order.

    Column ORDER varies by format family (classic AF MPL prints CAGE before
    DESCRIPTION; NWS/EHB manuals print REF DES after the index and CAGE after
    DESCRIPTION), so cells are sliced between each column's start and the next
    column's start in left-to-right order, whatever that order is.
    """

    starts: list[tuple[str, int]]  # (cell name, char offset) sorted by offset

    @property
    def pad(self) -> int:
        """Width every sliced line is padded to (past the last column start)."""
        return (self.starts[-1][1] if self.starts else 0) + 40

    def slice(self, line: str) -> dict[str, str]:
        out = dict.fromkeys(_CELL_NAMES, "")
        for pos, (name, start) in enumerate(self.starts):
            end = self.starts[pos + 1][1] if pos + 1 < len(self.starts) else None
            if not name.startswith("_"):  # "_*" columns only bound their neighbours
                out[name] = (
                    line[start:end].strip() if end is not None else line[start:].strip()
                )
        return out


def _header_anchors(window: list[str], *, rotated: bool = False) -> dict[str, int]:
    """Column-name -> leftmost char offset of its header token in ``window``."""

    def pos(pattern: str) -> int | None:
        best: int | None = None
        for w in window:
            m = re.search(pattern, w, re.I)
            if m and (best is None or m.start() < best):
                best = m.start()
        return best

    part = pos(r"\bPART\b")
    number = pos(r"\bNUMBER\b")
    ruler = pos(r"1\s?2\s?3\s?4\s?5\s?6\s?7")
    desc = pos(r"\bDESCRIPTION\b")
    units = pos(r"\bUNITS\b|\bQTY\b")
    assy = pos(r"\bASSY\b")

    anchors: dict[str, int] = {}
    if rotated:
        # Prefer the PART token itself: rotated banners stack "INDEX" over
        # "NUMBER" in the leftmost column, so a bare NUMBER may belong to the
        # index header.
        if part is not None:
            anchors["part"] = part
        elif number is not None:
            anchors["part"] = number
    elif part is not None or number is not None:
        anchors["part"] = min(x for x in (part, number) if x is not None)
    if ruler is not None:
        anchors["desc"] = ruler
    elif desc is not None:
        anchors["desc"] = desc
    if units is not None or assy is not None:
        anchors["units"] = min(x for x in (units, assy) if x is not None)
    # FSCM is the pre-1990s name for the CAGE code column.
    for name, pattern in (
        ("cage", r"\bCAGE\b|\bFSCM\b"),
        ("uoc", r"\bUSABLE\b"),
        ("smr", r"\bSMR\b"),
        # Uppercase only: prose ("reference designators") must not anchor it.
        ("refdes", r"(?-i:\bREF\s?DES\b)"),
        ("indent", r"(?-i:\bINDENT\b)"),
    ):
        if (found := pos(pattern)) is not None:
            anchors[name] = found
    return anchors


def _build_columns(
    lines: list[str], header_end: int, anchors: dict[str, int], *, rotated: bool = False
) -> _Columns:
    body_sample = [
        ln
        for ln in lines[header_end : header_end + 30]
        if ln.strip() and not _TERMINATOR.match(ln)
    ]
    # For rotated banners, snap left-to-right with each boundary strictly right
    # of the previous one, so adjacent stacked headers can't collapse two
    # columns onto one offset.
    ordered = sorted(anchors.items(), key=lambda item: item[1])
    starts: list[tuple[str, int]] = []
    floor = 0
    for name, anchor in ordered:
        # prefer_data everywhere: headers are centered over their data, so the
        # rightmost clean gutter can sit PAST short data (a 5-char part number
        # under a wide PART NUMBER banner leaves whitespace both sides); the
        # gutter where a sampled row actually starts is the real column edge.
        # The monotonic floor keeps data-less columns (a blank USABLE ON) from
        # collapsing onto their left neighbour's snapped start.
        snapped = _snap_boundary(body_sample, anchor, floor=floor, prefer_data=True)
        starts.append((name, snapped))
        floor = snapped
    # The figure/index cell is everything left of the first anchored column.
    return _Columns(starts=[("index", 0), *[(n, s) for n, s in starts if s > 0]])


def _find_header(  # noqa: C901 - two header layouts inline; splitting hurts clarity
    lines: list[str], start: int
) -> tuple[_Columns, int] | None:
    """Locate a parts-list header block at/after ``start``; return columns and
    the line index just past the header.

    Two layouts are recognized:

    - the classic AF MPL block: FIGURE & INDEX / PART NUMBER / CAGE /
      DESCRIPTION / UNITS PER ASSY headers within a few lines, and
    - the NWS/EHB rotated block (WSR-88D-style IPBs): the same column names
      spread across a taller banner because several headers print rotated,
      with REF DES and INDENT columns and QTY PER ASSY naming.
    """
    header_token = re.compile(
        r"\bFIGURE\b|\bPART\b|\bNUMBER\b|\bDESCRIPTION\b|\bCAGE\b|\bFSCM\b|"
        r"\bUNITS\b|\bQTY\b|\bASSY\b|\bUSABLE\b|\bSMR\b|\bREF\s*DES\b|\bINDENT\b|"
        r"\bCODES?\b|\bSHEET NO\b|1\s?2\s?3\s?4\s?5\s?6\s?7",
        re.I,
    )
    # Cross-reference cable/part tables (NWS/EHB): a single header line
    # "INDEX NO. | REF DES | PART NO. | NSN | ... | WHERE USED" with no
    # DESCRIPTION column. All tokens on one line keeps prose pages out.
    cable_header = re.compile(
        r"\bINDEX\s+NO\b.*\bPART\s+NO\b.*\bNSN\b.*\bWHERE\s+USED\b", re.I
    )
    for i in range(start, len(lines)):
        if cable_header.search(lines[i]):
            # Labels can wrap onto the neighbouring lines ("LENGTH" over
            # "(IN)"), so anchor across a one-line halo around the header.
            halo = lines[max(0, i - 1) : i + 2]

            def halo_pos(pattern: str) -> int | None:
                best: int | None = None
                for ln in halo:
                    m = re.search(pattern, ln, re.I)
                    if m and (best is None or m.start() < best):
                        best = m.start()
                return best

            anchors = {
                name: found
                for name, pattern in (
                    ("refdes", r"\bREF\s*DES\b"),
                    ("part", r"\bPART\s+NO\b"),
                    ("nsn", r"\bNSN\b"),
                    ("uoc", r"\bWHERE\s+USED\b"),
                    # Measurement/remark columns are bounds, not BOM data.
                    ("_drop", r"\bLENGTH\b|\bREMARKS\b|\bNOTES\b"),
                )
                if (found := halo_pos(pattern)) is not None
            }
            if {"part", "nsn"} <= set(anchors):
                # Body starts right after the header line; a wrapped label
                # line below it ("(IN)") slices into the drop column harmlessly.
                return _build_columns(lines, i + 1, anchors, rotated=True), i + 1
        if not _HEADER_FIGURE.search(lines[i]):
            continue
        # A figure CAPTION ("Figure 6-1. Title") also contains the word
        # Figure — it is never the header row.
        if _match_figure_caption(lines[i].strip()):
            continue
        # Classic block first; rotated NWS/EHB banner second. Rotated banners
        # print several column labels ABOVE the FIGURE line ("QTY PER ASSY",
        # "INDENT", "USABLE ON"), so that window extends upward too.
        for window_start, window_len in ((i, 4), (max(0, i - 8), 14)):
            window = lines[window_start : i + window_len]
            blob = "\n".join(window)
            has_desc = "DESCRIPTION" in blob.upper() or _HEADER_RULER.search(blob)
            anchors = _header_anchors(window, rotated=window_len > 4)
            if not has_desc or not {"part", "desc", "units"} <= set(anchors):
                continue
            if window_len == 4 and "refdes" in anchors:
                # REF DES is exclusively a rotated NWS/EHB banner column; its
                # labels stack taller than four lines, so the classic slice
                # would collapse the PART anchor onto the stacked "NUMBER".
                # Defer to the tall window.
                continue
            if window_len > 4 and (
                "refdes" not in anchors or _LOWERCASE_WORD.search(lines[i])
            ):
                # The tall window only accepts the distinctive rotated NWS/EHB
                # banner: an all-caps FIGURE banner line plus a REF DES column.
                # Prose/TOC pages that merely mention the column names carry
                # lowercase words on the FIGURE line and are rejected.
                continue

            if window_len == 4:
                end = i + len(window)
                for j in range(i, min(i + 4, len(lines))):
                    if re.search(
                        r"SHEET NO\.|CODE\s*$|1\s?2\s?3\s?4\s?5\s?6\s?7", lines[j], re.I
                    ):
                        end = j + 1
            else:
                # Rotated banners interleave blank lines: the header ends after
                # its last header-word line (data must not be swallowed).
                end = i + 1
                for j in range(i, min(i + window_len, len(lines))):
                    if header_token.search(lines[j]):
                        end = j + 1
            return _build_columns(lines, end, anchors, rotated=window_len > 4), end
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


def _snap_boundary(
    sample: list[str], anchor: int, floor: int = 0, prefer_data: bool = False
) -> int:
    """Snap a header-derived column anchor onto a real whitespace gutter.

    Headers are centered over their data, so data often starts left of the
    header text. A valid boundary is a character column that is whitespace in
    every sampled data line; search near the anchor, preferring the rightmost
    valid column at or just past it. With ``prefer_data`` (rotated banners,
    whose labels sit far from their data), prefer a gutter where some line's
    data actually STARTS, falling back to any gutter.
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

    def starts_data(c: int) -> bool:
        return any(c < len(ln) and ln[c] != " " for ln in sample)

    fallback: int | None = None
    for c in range(anchor + 2, max(anchor - 12, floor), -1):
        if not is_gutter(c):
            continue
        if not prefer_data or starts_data(c):
            return c
        if fallback is None:
            fallback = c
    if fallback is not None:
        return fallback
    return max(floor + 1, anchor - 2) if floor else max(0, anchor - 2)


def parse_parts_lists(  # noqa: C901 - single-pass MPL state machine; splitting hurts clarity
    pages: list[str], *, first_page_number: int = 1
) -> tuple[list[PartsListEntry], list[FigureRecord]]:
    """Parse every parts-list table across ``pages``.

    Returns (entries, figures). Page numbers are PDF 1-based.
    """
    entries: list[PartsListEntry] = []
    figures: list[FigureRecord] = []
    figure_index_by_key: dict[tuple[str, str], int] = {}
    seq = 0
    stack: dict[int, int] = {}  # indenture level -> sequence of latest entry
    last_at_level: dict[int, int] = {}
    current_figure = ""
    expect_figure_token = False
    ap_active = False
    ap_owner: int | None = None
    ap_level: int | None = None
    usable_on_legend_active = False

    for page_offset, page in enumerate(pages):
        page_number = first_page_number + page_offset
        lines = page.splitlines()
        last_content_line = max(
            (idx for idx, ln in enumerate(lines) if ln.strip()), default=-1
        )

        for ln in lines:
            if m := _match_figure_caption(ln.strip()):
                if _FIGURE_TOC_REFERENCE.search(m.group(2)):
                    continue
                title = _TRAILING_LEADERS.sub("", m.group(2)).strip().rstrip(".")
                # An untitled ("Figure 3-1") caption is still a real figure —
                # figure-only sheets often carry nothing but the number.
                if not title or (2 < len(title) < 90 and not title.endswith(",")):
                    key = (m.group(1), m.group(3) or "")
                    existing_index = figure_index_by_key.get(key)
                    if existing_index is None:
                        figure_index_by_key[key] = len(figures)
                        figures.append(
                            FigureRecord(
                                figure_number=m.group(1),
                                figure_title=title,
                                sheet_number=m.group(3) or "",
                                sheet_total=int(m.group(4)) if m.group(4) else None,
                                page_number=page_number,
                            )
                        )
                    elif title and not figures[existing_index].figure_title:
                        # A real titled caption supersedes an earlier bare
                        # reference for the same figure/sheet.
                        figures[existing_index] = FigureRecord(
                            figure_number=m.group(1),
                            figure_title=title,
                            sheet_number=m.group(3) or "",
                            sheet_total=int(m.group(4)) if m.group(4) else None,
                            page_number=page_number,
                        )

        cursor = 0
        while True:
            found = _find_header(lines, cursor)
            if not found:
                break
            cols, body_start = found
            cursor = body_start
            expect_figure_token = True
            column_names = {name for name, _ in cols.starts}
            has_indent = "indent" in column_names
            has_refdes = "refdes" in column_names

            i = body_start
            while i < len(lines):
                raw_line = lines[i].rstrip("\n")
                stripped = raw_line.strip()
                i += 1
                if not stripped:
                    continue
                if _DATE_FOOTER.match(stripped) or _NOTE_LINE.match(stripped):
                    continue
                if _TERMINATOR.match(stripped):
                    cursor = i
                    break
                if _HEADER_FIGURE.search(raw_line) and _find_header(lines, i - 1):
                    cursor = i - 1
                    break
                if m := _match_figure_caption(stripped):
                    current_figure = m.group(1)
                    continue
                if _AP_SEPARATOR.match(stripped):
                    ap_active = False
                    continue

                cells = cols.slice(raw_line.ljust(cols.pad))
                cage_cell, desc = _normalize_cage_and_description(
                    cells["cage"], cells["desc"]
                )
                if _AP_GROUP_MARKER.match(desc) or _AP_GROUP_MARKER.match(stripped):
                    ap_active = True
                    ap_owner = entries[-1].sequence if entries else None
                    ap_level = entries[-1].indenture_level if entries else None
                    continue

                has_payload = any(
                    cells[c]
                    for c in ("part", "cage", "units", "uoc", "smr", "refdes", "nsn")
                )
                if not has_payload and (banner := _FIGURE_BANNER.match(cells["index"])):
                    current_figure = banner.group(1)
                    continue
                # Dash-less banner: the figure number alone in the index column
                # as the table's first token ("6-6" straight under the header).
                # Only honored in that position — stray OCR digits and page
                # numbers elsewhere must not hijack the current figure.
                if (
                    expect_figure_token
                    and not has_payload
                    and not desc
                    and cells["index"]
                    and stripped == cells["index"]
                    and _FIGURE_TOKEN.match(cells["index"])
                    and i - 1 != last_content_line
                ):
                    current_figure = cells["index"]
                    expect_figure_token = False
                    continue
                if _USABLE_ON_LEGEND_HEADER.search(stripped):
                    usable_on_legend_active = True
                    continue
                if usable_on_legend_active:
                    if _USABLE_ON_LEGEND_ROW.match(stripped):
                        continue
                    usable_on_legend_active = False
                if not desc and not has_payload and not cells["index"]:
                    continue
                # Wrapped description text: no payload cells, continue previous.
                # Usable-on codes stack vertically, so a wrapped line may still
                # carry a UOC letter (and nothing else) — append it to the
                # entry's codes rather than treating the line as a new row.
                # A bare page number is footer noise, never wrapped prose.
                uoc_only_payload = (
                    has_payload
                    and cells["uoc"]
                    and re.fullmatch(r"[A-Z](?:\s+[A-Z])*", cells["uoc"])
                    and not any(
                        cells[c]
                        for c in ("part", "cage", "units", "smr", "refdes", "nsn")
                    )
                )
                if (
                    (not has_payload or uoc_only_payload)
                    and entries
                    and not _LEADING_DOTS.match(desc)
                    and (desc or uoc_only_payload)
                    and not (_BARE_PAGE_NUMBER.match(desc) and stripped == desc)
                ):
                    prev = entries[-1]
                    if desc:
                        prev.description_raw = f"{prev.description_raw} {desc}".strip()
                        if m := _NSN_IN_DESC.search(prev.description_raw):
                            prev.nsn_raw = re.sub(r"\s", "", m.group(1))
                    if uoc_only_payload:
                        prev.usable_on_code_raw = (
                            f"{prev.usable_on_code_raw} {cells['uoc']}".strip()
                        )
                    continue
                if not desc and not cells["part"]:
                    continue
                # Page-footer noise that slipped through column slicing.
                if (
                    not has_payload
                    and _PAGE_FOOTER.match(stripped)
                    and len(stripped) < 30
                ):
                    continue

                seq += 1
                level = _indenture_level(desc)
                # NWS/EHB tables encode indenture as a digit in the INDENT
                # column (or merged onto the description) instead of dots.
                if has_indent:
                    if cells["indent"].isdigit():
                        level = int(cells["indent"])
                    elif m := re.match(r"^(\d{1,2})\s+(\S.*)$", desc):
                        level = int(m.group(1))
                        desc = m.group(2)

                index_cell = cells["index"]
                if has_refdes and (
                    m := re.match(r"^(\d+[A-Z]?)-(\d+[A-Z]?)$", index_cell)
                ):
                    # NWS/EHB "figure-index" composite ("2-57" = figure 2, index 57).
                    current_figure = m.group(1)
                    figure_index = m.group(2)
                elif m := re.match(r"^(\d+(?:-\d+)?[A-Z]?)\s*-\s*$", index_cell):
                    # "1-" / "6-1-" form: figure number preceding the first index.
                    current_figure = m.group(1)
                    figure_index = ""
                elif (
                    m := re.match(r"^(\d+(?:-\d+)?[A-Z]?)-?\s+(\S.*)$", index_cell)
                ) and not m.group(2).startswith("-"):
                    # "1   6" form: figure number + index (page continuation).
                    # A dashed second token ("86   -85") is OCR bleed around a
                    # legacy "-NN" continuation index, not a figure switch.
                    current_figure = m.group(1)
                    figure_index = m.group(2).strip()
                elif expect_figure_token and re.match(
                    r"^\d+(?:-\d+)?[A-Z]?$", index_cell
                ):
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

                units = cells["units"]
                smr_cell = cells["smr"]
                uoc_cell = cells["uoc"]
                refdes_cell = cells["refdes"]
                part_cell = cells["part"]
                if has_refdes:
                    # Column snapping drifts on pages whose gutters are filled
                    # (change-marker asterisks, wide part numbers). Repair from
                    # the cells' own shapes — they are strongly typed: REF DES
                    # never looks like a part number, QTY is numeric, SMR is
                    # 2-6 letters, usable-on codes are single letters.
                    refdes_cell = refdes_cell.lstrip("* ").strip()
                    if not part_cell and refdes_cell:
                        tokens = refdes_cell.split()
                        if tokens and re.fullmatch(r"[0-9][0-9A-Z./-]{4,}", tokens[-1]):
                            part_cell = tokens[-1]
                            refdes_cell = " ".join(tokens[:-1])
                    if (
                        not uoc_cell
                        and (m := re.fullmatch(r"(\d+)\s+([A-Z]{2,6})", units))
                        and re.fullmatch(r"[A-Z]?", smr_cell)
                    ):
                        units, smr_cell, uoc_cell = m.group(1), m.group(2), smr_cell
                cells["part"], cells["refdes"] = part_cell, refdes_cell
                cells["units"], cells["smr"], cells["uoc"] = units, smr_cell, uoc_cell

                row_type = "part"
                if level == 0:
                    row_type = "end-item"
                if units.upper() == "REF":
                    row_type = "ref"
                if re.match(r"^KIT[, ]", desc.lstrip(". ").upper()):
                    row_type = "kit"
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
                    reference_designator_raw=cells["refdes"],
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
                elif cells["nsn"]:
                    entry.nsn_raw = re.sub(r"\s", "", cells["nsn"])
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
