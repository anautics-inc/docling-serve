"""Part-name attribute mining (item name, qualifiers, spec family)."""

from __future__ import annotations

import pytest

from docling_serve.technical_order.mpl import PartsListEntry
from docling_serve.technical_order.part_attributes import (
    classify_spec_family,
    parse_part_name,
)


@pytest.mark.parametrize(
    ("nomenclature", "item_name", "modifiers"),
    [
        ("SCREW, Cap, hex head", "SCREW", ["Cap", "hex head"]),
        ("MOTOR GENERATOR SET, Brushless,", "MOTOR GENERATOR SET", ["Brushless"]),
        ("LOCKWASHER", "LOCKWASHER", []),
        ("SCREW, Cap, hex head (AP)", "SCREW", ["Cap", "hex head"]),
        (
            "STATOR, Exciter field, motor . . . . . . . 1",
            "STATOR",
            ["Exciter field", "motor"],
        ),
        ("", "", []),
        ("   ", "", []),
    ],
)
def test_parse_part_name(nomenclature, item_name, modifiers):
    parsed = parse_part_name(nomenclature)
    assert parsed["itemName"] == item_name
    assert parsed["itemModifiers"] == modifiers


@pytest.mark.parametrize(
    ("part_number", "family"),
    [
        ("MS90728-209", "MS"),
        ("NAS1149-D0332", "NAS"),
        ("AN960-10", "AN"),
        ("JAN1N4148", "JAN"),
        ("MIL-DTL-38999", "MIL"),
        ("21435", ""),
        ("19300-3", ""),
        ("", ""),
        ("MSXRAY", ""),  # MS not followed by a digit is not a spec number
    ],
)
def test_classify_spec_family(part_number, family):
    assert classify_spec_family(part_number) == family


def test_entry_dict_carries_mined_attributes():
    entry = PartsListEntry(
        sequence=4,
        page_number=9,
        part_number_raw="MS90728-209",
        nomenclature="SCREW, Cap, hex head",
        description_raw="SCREW, Cap, hex head (AP) . . . . 4",
    )
    d = entry.as_dict()
    assert d["itemName"] == "SCREW"
    assert d["itemModifiers"] == ["Cap", "hex head"]
    assert d["specFamily"] == "MS"
    # Raw fields untouched.
    assert d["nomenclature"] == "SCREW, Cap, hex head"
    assert d["descriptionRaw"].startswith("SCREW, Cap, hex head (AP)")
