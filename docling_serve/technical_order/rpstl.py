"""RPSTL (Repair Parts and Special Tools List) parts-list parser.

RPSTL is the Army/joint-service parts grammar (TM ...-23&P, AF TO-RPSTL). Unlike
the AF MPL "FIGURE & INDEX" table, an RPSTL parts page is a fixed column layout:

    (1)    (2) SMR CODE              (3)    (4)            (5)            (6)  (7)
    ITEM   ARMY  AF   NAVY  USMC     FSCM   PART NUMBER    DESCRIPTION    QTY  UOC
     1     PAOZZ PAOZZ      PAOZZ    0W357  4601-6         . 1/2 HEX NUT  1    10
                                            5310-00-596-2573

Each part spans two printed lines: the row itself, then the NSN under the PART
NUMBER column. Indenture is the count of leading dots in the description. Columns
are sliced by the positions of the header tokens (robust to per-TO spacing),
which ``pdftotext -layout`` preserves. Produces the same ``PartsListEntry`` the
MPL parser emits, so everything downstream (BOM, entities, graph) is unchanged.
"""

from __future__ import annotations

import re

from docling_serve.technical_order.mpl import FigureRecord, PartsListEntry

# Header anchor: the row carrying the column titles. Requires the CAGE column
# (FSCM on AF/older RPSTL, CAGEC on Army modern RPSTL) plus PART + DESCRIPTION so
# a stray "PART NUMBER" in prose can't be mistaken for the table head.
_HEADER_RE = re.compile(r"\b(?:FSCM|CAGEC)\b.*\bPART\b.*\bDESCRIPTION\b", re.I)
_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")
_NSN_TOKEN_RE = re.compile(r"^\d{4}-\d{2}-\d{3}-\d{4}$")
#: CAGE / FSCM code: 5 alphanumerics containing at least one digit (so it is not
#: confused with a 5-letter SMR code or an all-letter word).
_CAGE_RE = re.compile(r"^(?=[0-9A-Z]{5}$)[0-9A-Z]*[0-9][0-9A-Z]*$")
#: SMR (Source/Maintenance/Recoverability) code: 4-5 letters, e.g. PAOZZ, XB.
_SMR_RE = re.compile(r"^[A-Z]{2,5}$")
_ITEM_RE = re.compile(r"^\d{1,4}[A-Z]?$")
_INT_RE = re.compile(r"^\d{1,4}$")


def _parse_row(line: str) -> dict | None:
    """Token-based parse of one RPSTL data row.

    Layout: [item] [SMR ...] CAGE PART DESCRIPTION [QTY] [UOC]. Column spacing
    drifts per TO, so we walk tokens left-to-right anchored on the CAGE (a 5-char
    code with a digit) rather than fixed offsets. Returns a field dict or None.
    """
    tokens = line.split()
    if not tokens:
        return None
    i = 0
    item = ""
    if _ITEM_RE.match(tokens[0]) and line[: line.find(tokens[0]) + 1].strip(" ") != "":
        # Leading item number sits in the far-left column.
        if line.index(tokens[0]) <= 8:
            item = tokens[0]
            i = 1
    smr = ""
    while i < len(tokens) and _SMR_RE.match(tokens[i]):
        if not smr:
            smr = tokens[i]
        i += 1
    # Optional inline NSN column (Army modern RPSTL: ITEM SMR NSN CAGEC PART ...).
    # AF/older RPSTL has no NSN column here (NSN is on the continuation line).
    nsn = ""
    if i < len(tokens) and _NSN_TOKEN_RE.match(tokens[i]):
        nsn = tokens[i]
        i += 1
    if i >= len(tokens) or not _CAGE_RE.match(tokens[i]):
        return None  # no CAGE where one must be -> not a part row
    cage = tokens[i]
    i += 1
    if i >= len(tokens):
        return None
    part = tokens[i]
    i += 1
    rest = tokens[i:]
    # Trailing QTY + UOC: the last one/two standalone integers.
    qty = uoc = ""
    while rest and _INT_RE.match(rest[-1]) and len([t for t in (qty, uoc) if t]) < 2:
        if not uoc:
            uoc = rest.pop()
        elif not qty:
            qty = rest.pop()
        else:
            break
    if qty == "" and uoc:
        qty, uoc = uoc, ""
    desc_tokens = rest
    indenture = 0
    while desc_tokens and desc_tokens[0] == ".":
        indenture += 1
        desc_tokens.pop(0)
    description = " ".join(desc_tokens).lstrip(". ").strip()
    if not indenture:
        lead = " ".join(rest)[: len(" ".join(rest)) - len(" ".join(rest).lstrip("."))]
        indenture = lead.count(".")
    return {
        "item": item,
        "smr": smr,
        "nsn": nsn,
        "cage": cage,
        "part": part,
        "description": description,
        "qty": qty,
        "uoc": uoc,
        "indenture": indenture,
    }


def parse_rpstl(
    pages: list[str], *, first_page_number: int = 1
) -> tuple[list[PartsListEntry], list[FigureRecord]]:
    """Parse every RPSTL parts table across ``pages`` -> (entries, figures)."""
    entries: list[PartsListEntry] = []
    figures: list[FigureRecord] = []
    seq = 0
    active = False
    current_group = ""
    last: PartsListEntry | None = None

    for page_offset, page in enumerate(pages):
        page_number = first_page_number + page_offset
        for raw in page.splitlines():
            if _HEADER_RE.search(raw):
                active = True
                continue
            if not active or not raw.strip():
                continue

            # NSN continuation line (NSN alone under the part column).
            stripped = raw.strip()
            nsn_hit = _NSN_RE.search(stripped)
            if nsn_hit and last is not None and not _parse_row(raw):
                if not last.nsn_raw:
                    last.nsn_raw = nsn_hit.group(0)
                continue

            row = _parse_row(raw)
            if row is None:
                # Possibly an assembly/group title (free text, no CAGE) — keep as figure.
                text = stripped.lstrip(". ").strip()
                if 3 < len(text) < 60 and text.isupper():
                    current_group = text
                continue

            entry = PartsListEntry(
                sequence=seq,
                page_number=page_number,
                figure_number_raw=current_group,
                figure_index_raw=row["item"] if _ITEM_RE.match(row["item"] or "") else "",
                part_number_raw=row["part"],
                cage_raw=row["cage"],
                description_raw=row["description"],
                units_per_assembly_raw=row["qty"],
                usable_on_code_raw=row["uoc"],
                smr_raw=row["smr"],
                nsn_raw=row["nsn"],
                indenture_level=row["indenture"],
                row_type="part",
            )
            entries.append(entry)
            last = entry
            seq += 1

    return entries, figures
