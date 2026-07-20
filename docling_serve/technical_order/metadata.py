"""TO identity from the title page and List of Effective Pages.

Deterministic regex parsing for born-digital documents — the AF title page is
highly conventional. Every captured value is verbatim from the page; nothing
is normalized away (the ``toNumberRaw`` / parsed split happens downstream).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TO_NUMBER = re.compile(
    r"T\.?\s?O\.?\s+([0-9]{1,2}[A-Z]?[A-Z0-9]*(?:-[0-9A-Z.]+){1,5})"
)
_NSN = re.compile(r"NSN[:\s]+([0-9]{4}[- ]?[0-9]{2}[- ]?[0-9]{3}[- ]?[0-9]{4})")
_PN = re.compile(r"\bP\/?N[:\s]+([A-Z0-9][A-Z0-9./-]{2,})")
_DISTRIBUTION = re.compile(
    r"(DISTRIBUTION STATEMENT\s+([A-FX])[^\n]*(?:\n[^\n]+){0,3}?)(?:\n\s*\n|$)", re.I
)
_PA_CASE = re.compile(r"PA Case Number[:\s]+([0-9A-Z-]+)", re.I)
_REFERRED = re.compile(r"referred to ([0-9A-Z][0-9A-Z /-]{3,40}?)[,.]")
_SUPERSEDES = re.compile(r"^[^\n]*supersed[^\n]*(?:\n[^\n]+){0,2}", re.I | re.M)
_SUPERSEDES_TO = re.compile(r"supersedes\s+(?:TO|T\.O\.)\s+([0-9A-Z.-]+)", re.I)
_AMENDS = re.compile(r"supplements\s+(?:TO|T\.O\.)\s+([0-9A-Z.-]+)", re.I)
# The issue date prints on its own line, OR shares the footer line with the
# change block on merged publications ("25 OCTOBER 2012        CHANGE 3 - 20 MAY 2015").
_PUB_DATE = re.compile(
    r"^\s*(\d{1,2}\s+[A-Z]{3,9}\s+\d{4})\s*(?:CHANGE\s+\d+.*)?$", re.M
)
_CHANGE_TITLE = re.compile(
    r"CHANGE\s+(\d+)\s*[-\u2013\u2014]\s*(\d{1,2}\s+[A-Z]+\s+\d{4})", re.I
)
_BASIC_DATE_LEP = re.compile(r"Original\s*[.\s]*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})")
_MANUFACTURER = re.compile(r"(?:manufactured|prepared)\s+by\s+([^.,\n]{4,60})", re.I)
_CONTRACTS = re.compile(
    r"[Cc]ontract\s+(?:[Nn]umbers?|[Nn]o\.?s?)?\s*([A-Z0-9()\-]+(?:\s+and\s+[A-Z0-9()\-]+)*)"
)
# Bare DoD contract numbers printed under the manufacturer block
# ("F41608-93-D-0526", "FA8518-14-C-0006", "F04606-83-D-0026").
_BARE_CONTRACT = re.compile(r"\b([A-Z]{1,2}[0-9]{4,6}-[0-9]{2}-[A-Z]-[0-9]{4})\b")
# "PART NO. 0015-70029" variant of the end-item part number line.
_PART_NO = re.compile(r"\bPART\s+NO\.?[:\s]+([A-Z0-9][A-Z0-9./-]{2,})", re.I)
# Maintenance-level qualifier printed between the manual-type headline and the
# nomenclature ("INTERMEDIATE LEVEL", "ORGANIZATIONAL", "ORGANIZATIONAL AND
# INTERMEDIATE MAINTENANCE") — identity metadata, not part of the item name.
_MAINT_LEVEL_LINE = re.compile(
    r"^(?:ORGANIZATIONAL|INTERMEDIATE|DEPOT|FIELD)"
    r"(?:(?:\s*(?:,|AND|/)\s*)(?:ORGANIZATIONAL|INTERMEDIATE|DEPOT|FIELD))*"
    r"(?:\s+(?:LEVEL|MAINTENANCE))?$",
    re.I,
)
# End-item model designators ("MODEL 30D36R", "MODEL FDECU-2, FDECU-3 AND FDECU-9").
_MODEL_LINE = re.compile(
    r"^MODELS?\s+([A-Z0-9][A-Z0-9 ,/()-]*(?:AND\s+[A-Z0-9-]+)?)$", re.I
)
# "CONTRACT 50-DMNW-8-00032" / "CONTRACT NUMBER: FA8540-12-C-0025" labeled lines.
_CONTRACT_LABELED = re.compile(
    r"^CONTRACT(?:\s+(?:NUMBERS?|NOS?\.?))?[:\s]+([A-Z0-9][A-Z0-9-]{5,})", re.I | re.M
)
# Labeled cover-sheet fields (the "IDENTIFYING TECHNICAL PUBLICATION SHEET"
# layout used when a commercial manual is adopted for Air Force use).
_LABELED_FIELD = re.compile(r"^([A-Z][A-Z ]{3,40}?)\s*[:\u2013-]\s+(\S.*)$")

#: Manual-type headline phrases, most specific first.
_TYPE_LINES = [
    "OPERATION, MAINTENANCE, AND OVERHAUL INSTRUCTIONS WITH ILLUSTRATED PARTS BREAKDOWN",
    "OPERATIONAL AND MAINTENANCE INSTRUCTIONS WITH ILLUSTRATED PARTS BREAKDOWN",
    "OPERATION AND MAINTENANCE INSTRUCTIONS WITH ILLUSTRATED PARTS BREAKDOWN",
    "OPERATIONS AND MAINTENANCE INSTRUCTIONS WITH ILLUSTRATED PARTS BREAKDOWN",
    "OPERATION AND MAINTENANCE INSTRUCTIONS",
    "OPERATIONAL AND MAINTENANCE INSTRUCTIONS",
    "OPERATION AND MAINTENANCE MANUAL",
    "OPERATIONS AND MAINTENANCE MANUAL",
    "OPERATIONS AND MAINTENANCE",
    "OPERATION AND MAINTENANCE",
    "OVERHAUL WITH PARTS BREAKDOWN",
    "INSTRUCTIONS AND PARTS BREAKDOWN",
    "ILLUSTRATED PARTS BREAKDOWN",
    "REPAIR PARTS AND SPECIAL TOOLS LIST",
    "OVERHAUL INSTRUCTIONS",
    "MAINTENANCE INSTRUCTIONS",
]

#: Title-block lines that are structure, not nomenclature.
_STRUCTURE_LINES = {"TECHNICAL MANUAL", "WORK PACKAGE", "TECHNICAL ORDER"}


@dataclass(slots=True)
class TOMetadata:
    to_number_raw: str = ""
    document_number: str = ""
    document_title: str = ""
    manual_type: str = ""
    publication_date: str = ""
    basic_date: str = ""
    change_level: str = ""
    change_date: str = ""
    supersedure_text_raw: str = ""
    supersedes_to_number: str = ""
    amends_to_number: str = ""
    end_item_part_number_raw: str = ""
    end_item_nsn_raw: str = ""
    maintenance_level: str = ""
    models_raw: str = ""
    manufacturer_name_raw: str = ""
    contract_numbers_raw: str = ""
    distribution_statement: str = ""
    pa_case_number: str = ""
    managing_organization: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "toNumberRaw": self.to_number_raw,
            "documentNumber": self.document_number,
            "documentTitle": self.document_title,
            "manualType": self.manual_type,
            "publicationDate": self.publication_date,
            "basicDate": self.basic_date,
            "changeLevel": self.change_level,
            "changeDate": self.change_date,
            "supersedureTextRaw": self.supersedure_text_raw,
            "supersedesToNumber": self.supersedes_to_number,
            "amendsToNumber": self.amends_to_number,
            "endItemPartNumberRaw": self.end_item_part_number_raw,
            "endItemNsnRaw": self.end_item_nsn_raw,
            "maintenanceLevel": self.maintenance_level,
            "modelsRaw": self.models_raw,
            "manufacturerNameRaw": self.manufacturer_name_raw,
            "contractNumbersRaw": self.contract_numbers_raw,
            "distributionStatement": self.distribution_statement,
            "paCaseNumber": self.pa_case_number,
            "managingOrganization": self.managing_organization,
            "notes": self.notes,
        }


def parse_to_metadata(  # noqa: C901
    pages: list[str], *, filename: str = ""
) -> TOMetadata:
    """Parse identity fields from the first few pages of a TO."""
    meta = TOMetadata()
    title_page = pages[0] if pages else ""
    front = "\n".join(pages[:4])

    m = _TO_NUMBER.search(title_page) or _TO_NUMBER.search(front)
    if m:
        meta.to_number_raw = m.group(0).strip()
        meta.document_number = m.group(1).strip()
    elif filename:
        stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
        meta.document_number = stem
        meta.notes.append("TO number taken from filename")

    for phrase in _TYPE_LINES:
        if re.search(re.escape(phrase).replace(r"\ ", r"\s+"), title_page, re.I):
            meta.manual_type = phrase.title()
            break

    meta.document_title, meta.maintenance_level, meta.models_raw = _title_block(
        title_page, meta.manual_type
    )

    if m := _NSN.search(title_page):
        meta.end_item_nsn_raw = m.group(1).strip()
    if m := _PN.search(title_page):
        meta.end_item_part_number_raw = m.group(1).strip()
    elif m := _PART_NO.search(title_page):
        meta.end_item_part_number_raw = m.group(1).strip()
    if m := _DISTRIBUTION.search(title_page):
        meta.distribution_statement = re.sub(r"\s+", " ", m.group(1)).strip()
    if m := _PA_CASE.search(title_page):
        meta.pa_case_number = m.group(1).strip()
    if m := _REFERRED.search(title_page):
        meta.managing_organization = m.group(1).strip()
    if m := _SUPERSEDES.search(title_page):
        meta.supersedure_text_raw = re.sub(r"\s+", " ", m.group(0)).strip()
        if m2 := _SUPERSEDES_TO.search(meta.supersedure_text_raw):
            meta.supersedes_to_number = m2.group(1).strip()
    if m := _AMENDS.search(front):
        meta.amends_to_number = m.group(1).strip()
    if m := _PUB_DATE.search(title_page):
        meta.publication_date = m.group(1).strip()
    if m := _CHANGE_TITLE.search(front):
        meta.change_level = m.group(1)
        meta.change_date = m.group(2).strip()
    if m := _BASIC_DATE_LEP.search(front):
        meta.basic_date = m.group(1).strip()
    if m := _MANUFACTURER.search(front):
        meta.manufacturer_name_raw = m.group(1).strip()
    if m := _CONTRACTS.search(front):
        candidate = m.group(1).strip()
        # contract numbers are long alphanumerics; skip false hits like "F41608"
        if len(candidate) >= 8:
            meta.contract_numbers_raw = candidate
    # Labeled cover-sheet fields (the adoption sheet for commercial manuals and
    # any title page that prints "FIELD: value" identity lines).
    labeled: dict[str, str] = {}
    for ln in title_page.splitlines():
        if lm := _LABELED_FIELD.match(ln.strip()):
            labeled[lm.group(1).strip().upper()] = lm.group(2).strip()
    if equipment := labeled.get("EQUIPMENT"):
        if not meta.document_title or meta.document_title.upper().endswith(
            "PUBLICATION SHEET"
        ):
            meta.document_title = equipment
    if not meta.models_raw:
        meta.models_raw = labeled.get("MODEL NUMBERS", labeled.get("MODEL NUMBER", ""))
    if not meta.manufacturer_name_raw:
        meta.manufacturer_name_raw = labeled.get("MANUFACTURER", "")

    # Bare + labeled contract numbers under the manufacturer block (standard AF
    # title-page layout: manufacturer name line, then one line per contract).
    contracts = _dedupe(
        _BARE_CONTRACT.findall(title_page) + _CONTRACT_LABELED.findall(title_page)
    )
    if contracts:
        meta.contract_numbers_raw = ", ".join(contracts)
        if not meta.manufacturer_name_raw:
            meta.manufacturer_name_raw = _line_above_first_contract(title_page)

    return meta


def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value.strip())
    return list(seen)


def _line_above_first_contract(title_page: str) -> str:
    """Manufacturer fallback: the line printed directly above the first bare
    contract number (the conventional AF title-page manufacturer block)."""
    lines = [ln.strip() for ln in title_page.splitlines()]
    for i, ln in enumerate(lines):
        if _BARE_CONTRACT.search(ln) or _CONTRACT_LABELED.match(ln):
            for prev in reversed(lines[:i]):
                if not prev:
                    continue
                if len(prev) > 60 or ":" in prev or _BARE_CONTRACT.search(prev):
                    return ""
                if re.match(r"^(NSN|PN|P/N|PART\s+NO|MODEL|TO\s|THIS )", prev, re.I):
                    return ""
                return prev
            return ""
    return ""


#: A title-block line that ends the nomenclature (identity/prose fields below it).
_TITLE_TERMINATOR = re.compile(
    r"^(NSN\b|PN\b|P/N\b|PART\s+NO\b|DISTRIBUTION\b|THIS |BASIC AND ALL|"
    r"Manual Prepared|Published Under|CONTRACT\b|OFFICE OF\b)",
    re.I,
)

#: A connector-only headline wrap fragment ("WITH", "AND", "FOR").
_CONNECTOR_LINE = re.compile(r"^(WITH|AND|FOR)$", re.I)

#: Trailing headline suffix embedded in a nomenclature line
#: ("... (MCFTM) WITH ILLUSTRATED PARTS BREAKDOWN").
_TITLE_TYPE_SUFFIX = re.compile(r"\s+WITH\s+ILLUSTRATED\s+PARTS\s+BREAKDOWN\s*$", re.I)


def _title_block(  # noqa: C901 - linear title-block state machine; clearer inline
    title_page: str, manual_type: str = ""
) -> tuple[str, str, str]:
    """Parse the nomenclature block of the conventional AF title page.

    Everything between the headline block ("TECHNICAL MANUAL" / "WORK PACKAGE" /
    manual-type phrases) and the identity fields (PN / NSN / MODEL / contract /
    distribution prose) is the item name. Maintenance-level qualifiers and MODEL
    lines inside the block are captured separately — they are metadata, not the
    name. Returns ``(title, maintenance_level, models)``.
    """
    lines = [ln.strip() for ln in title_page.splitlines()]
    nonblank_next: list[str] = [""] * len(lines)
    following = ""
    for i in range(len(lines) - 1, -1, -1):
        nonblank_next[i] = following
        if lines[i]:
            following = lines[i]
    type_phrases = [
        re.compile("^" + re.escape(p).replace(r"\ ", r"\s+") + "$", re.I)
        for p in _TYPE_LINES
    ]

    # The headline often wraps mid-phrase ("OPERATION, MAINTENANCE, AND OVERHAUL
    # / INSTRUCTIONS WITH / ILLUSTRATED PARTS BREAKDOWN"), so mark every line
    # any manual-type phrase's whole-page match spans as headline structure.
    headline_lines: set[int] = set()
    leftover_ok = re.compile(r"^(WITH|AND|FOR)?$", re.I)
    for phrase in _TYPE_LINES:
        pattern = re.escape(phrase).replace(r"\ ", r"\s+")
        for m in re.finditer(pattern, title_page, re.I):
            first = title_page.count("\n", 0, m.start())
            last = title_page.count("\n", 0, m.end())
            # A boundary line only counts as headline when the phrase covers
            # it entirely (bar wrap connectors) — "(MCFTM) WITH ILLUSTRATED
            # PARTS BREAKDOWN" keeps its nomenclature prefix.
            line_start = title_page.rfind("\n", 0, m.start()) + 1
            line_end = title_page.find("\n", m.end())
            if line_end == -1:
                line_end = len(title_page)
            before = title_page[line_start : m.start()].strip()
            after = title_page[m.end() : line_end].strip()
            span = set(range(first, last + 1))
            if not leftover_ok.match(before):
                span.discard(first)
            if not leftover_ok.match(after):
                span.discard(last)
            headline_lines.update(span)

    def is_headline(line: str, index: int) -> bool:
        """A manual-type phrase line, allowing wrap connectors on either side
        ("WITH ILLUSTRATED PARTS BREAKDOWN", "... INSTRUCTIONS WITH")."""
        if index in headline_lines:
            return True
        candidate = re.sub(r"^(WITH|AND|FOR)\s+", "", line, flags=re.I)
        candidate = re.sub(r"\s+(WITH|AND|FOR)$", "", candidate, flags=re.I)
        return bool(candidate) and any(p.match(candidate) for p in type_phrases)

    picked: list[str] = []
    levels: list[str] = []
    models: list[str] = []
    model_continues = False
    started = False
    for i, ln in enumerate(lines):
        if not ln:
            model_continues = False
            continue
        upper = ln.upper()
        if _TO_NUMBER.fullmatch(ln) or upper in _STRUCTURE_LINES:
            started = True
            continue
        if not started:
            continue
        if (
            _TITLE_TERMINATOR.match(ln)
            or _BARE_CONTRACT.search(ln)
            or re.fullmatch(r"\d{1,2}\s+[A-Z]{3,9}\s+\d{4}", ln)
        ):
            if picked or levels or models:
                break
            continue
        if model_match := _MODEL_LINE.match(ln):
            models.append(model_match.group(1).strip())
            model_continues = ln.rstrip().upper().endswith(("AND", ","))
            continue
        if model_continues:
            models[-1] = f"{models[-1]} {ln}".strip()
            model_continues = ln.rstrip().upper().endswith(("AND", ","))
            continue
        if _MAINT_LEVEL_LINE.match(ln):
            levels.append(ln)
            continue
        if is_headline(ln, i) or _CONNECTOR_LINE.match(ln):
            continue
        # The manufacturer block: a short line printed directly above a contract
        # number is the company name, not part of the nomenclature.
        follower = nonblank_next[i]
        if _BARE_CONTRACT.search(follower) or _CONTRACT_LABELED.match(follower):
            continue
        if upper == ln and re.search(r"[A-Z]{3}", ln) and len(ln) < 90:
            picked.append(_TITLE_TYPE_SUFFIX.sub("", ln).strip())
        elif picked:
            break

    title = _collapse_repeats(" ".join(part for part in picked if part))
    return title, " ".join(levels).strip(), ", ".join(models).strip()


def _collapse_repeats(text: str) -> str:
    """Collapse immediately repeated words (side-banner artifacts like
    "WSR-88D WSR-88D" that pdftotext merges onto the nomenclature lines)."""
    words = text.split()
    out: list[str] = []
    for word in words:
        if not out or out[-1] != word:
            out.append(word)
    return " ".join(out).strip()
