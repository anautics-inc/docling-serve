"""Mine structured attributes out of parts-list nomenclature and numbers.

Cataloging convention (FLIS-style approved item names) writes descriptions
as ``NOUN, modifier, modifier`` — e.g. ``SCREW, Cap, hex head``. The leading
noun phrase is the item name; the comma-separated tail qualifies it. Pure
positional parsing — deterministic, no model calls — so 500+ entries per
document cost nothing and the output is testable.

Spec-family classification mirrors the prefixes hydration already treats as
standard hardware (MS/NAS/AN/JAN/MIL): for those the part number alone is
the identity, any qualified source may manufacture them.
"""

from __future__ import annotations

import re

__all__ = ["classify_spec_family", "parse_part_name"]

#: Government/industry standard prefixes; keep in sync with
#: captify-pytology ``bom_hydration._STANDARD_PART``.
_SPEC_FAMILIES = (
    ("MS", re.compile(r"^MS\d", re.I)),
    ("NAS", re.compile(r"^NAS\d", re.I)),
    ("AN", re.compile(r"^AN\d", re.I)),
    ("JAN", re.compile(r"^JAN\d", re.I)),
    ("MIL", re.compile(r"^MIL[-A-Z0-9]", re.I)),
)

#: Trailing parenthetical markers that are annotations, not name content:
#: (AP) attaching part, (NHA) next higher assembly, usable-on codes.
_TRAILING_MARKER = re.compile(r"\s*\((?:AP|NHA|[A-Z]{1,3}\d{0,2})\)\s*$")
#: Dot leaders / column bleed sometimes survive row reconstruction.
_DOT_LEADER = re.compile(r"(?:\s*\.){3,}.*$")
_WS = re.compile(r"\s+")


def classify_spec_family(part_number: str) -> str:
    """``MS90728-209`` -> ``MS``; unrecognized/vendor numbers -> ``""``."""
    pn = (part_number or "").strip()
    for family, pattern in _SPEC_FAMILIES:
        if pattern.match(pn):
            return family
    return ""


def parse_part_name(nomenclature: str) -> dict:
    """Split a nomenclature string into ``{itemName, itemModifiers}``.

    ``SCREW, Cap, hex head`` -> item name ``SCREW``, modifiers
    ``["Cap", "hex head"]``. Annotation parentheticals and dot leaders are
    stripped before splitting; the raw string is never modified upstream.
    """
    text = _WS.sub(" ", (nomenclature or "").strip())
    text = _DOT_LEADER.sub("", text)
    while True:
        stripped = _TRAILING_MARKER.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = text.strip(" .,")
    if not text:
        return {"itemName": "", "itemModifiers": []}
    head, *tail = [seg.strip(" .") for seg in text.split(",")]
    modifiers = [seg for seg in tail if seg]
    return {"itemName": head, "itemModifiers": modifiers}
