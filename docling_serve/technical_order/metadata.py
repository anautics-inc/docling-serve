"""TO identity from the title page and List of Effective Pages.

Deterministic regex parsing for born-digital documents — the AF title page is
highly conventional. Every captured value is verbatim from the page; nothing
is normalized away (the ``toNumberRaw`` / parsed split happens downstream).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TO_NUMBER = re.compile(r"T\.?\s?O\.?\s+([0-9]{1,2}[A-Z]?[A-Z0-9]*(?:-[0-9A-Z.]+){1,5})")
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
_PUB_DATE = re.compile(r"^\s*(\d{1,2}\s+[A-Z]{3,9}\s+\d{4})\s*$", re.M)
_CHANGE_TITLE = re.compile(r"CHANGE\s+(\d+)\s*[-–—]\s*(\d{1,2}\s+[A-Z]+\s+\d{4})", re.I)
_BASIC_DATE_LEP = re.compile(r"Original\s*[.\s]*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})")
_MANUFACTURER = re.compile(r"manufactured by\s+([^.,\n]{4,60})", re.I)
_CONTRACTS = re.compile(r"[Cc]ontract\s+(?:[Nn]umbers?|[Nn]o\.?s?)?\s*([A-Z0-9()\-]+(?:\s+and\s+[A-Z0-9()\-]+)*)")

#: Manual-type headline phrases, most specific first.
_TYPE_LINES = [
    "OPERATION AND MAINTENANCE INSTRUCTIONS WITH ILLUSTRATED PARTS BREAKDOWN",
    "OVERHAUL WITH PARTS BREAKDOWN",
    "INSTRUCTIONS AND PARTS BREAKDOWN",
    "ILLUSTRATED PARTS BREAKDOWN",
    "REPAIR PARTS AND SPECIAL TOOLS LIST",
    "OVERHAUL INSTRUCTIONS",
    "MAINTENANCE INSTRUCTIONS",
]


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
            "manufacturerNameRaw": self.manufacturer_name_raw,
            "contractNumbersRaw": self.contract_numbers_raw,
            "distributionStatement": self.distribution_statement,
            "paCaseNumber": self.pa_case_number,
            "managingOrganization": self.managing_organization,
            "notes": self.notes,
        }


def parse_to_metadata(pages: list[str], *, filename: str = "") -> TOMetadata:
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

    meta.document_title = _title_lines(title_page, meta.manual_type)

    if m := _NSN.search(title_page):
        meta.end_item_nsn_raw = m.group(1).strip()
    if m := _PN.search(title_page):
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

    return meta


_LEVEL_QUALIFIER = re.compile(
    r"^(ORGANIZATIONAL|INTERMEDIATE|DEPOT|FIELD)\b.*\bLEVEL\b", re.I
)


def _title_lines(title_page: str, manual_type: str) -> str:
    """Nomenclature block: uppercase lines after the manual-type headline.

    The headline often wraps across lines ("... INSTRUCTIONS / WITH /
    ILLUSTRATED PARTS BREAKDOWN"), so locate it in the full page text and
    start after the line where the phrase ENDS. Maintenance-level qualifiers
    ("INTERMEDIATE LEVEL") between the headline and the nomenclature are
    skipped — they aren't the item name.
    """
    lines = [ln.strip() for ln in title_page.splitlines()]
    start = 0
    if manual_type:
        pattern = re.escape(manual_type).replace(r"\ ", r"\s+")
        if m := re.search(pattern, title_page, re.I):
            start = title_page.count("\n", 0, m.end()) + 1
    picked: list[str] = []
    for ln in lines[start : start + 10]:
        if not ln:
            if picked:
                break
            continue
        if re.match(r"^(NSN|PN|P/N|DISTRIBUTION|THIS )", ln, re.I):
            break
        if _LEVEL_QUALIFIER.match(ln) and not picked:
            continue
        if ln.upper() == ln and re.search(r"[A-Z]{3}", ln) and len(ln) < 80:
            picked.append(ln)
        elif picked:
            break
    return " ".join(picked).strip()
