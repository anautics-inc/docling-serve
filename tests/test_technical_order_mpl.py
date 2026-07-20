"""MPL parser unit tests for column layouts beyond the classic AF-MPL block.

``parse_parts_lists`` reads layout-preserved page text, so these exercise the
NWS/EHB rotated banner (REF DES + INDENT columns, figure-index composites),
the cable cross-reference grid (NSN column, no DESCRIPTION), the QTY column
alias, and the footer/banner noise guards — all without a real PDF.
"""

from __future__ import annotations

from docling_serve.technical_order.mpl import parse_parts_lists


def _row(columns: dict[str, int], cells: dict[str, str]) -> str:
    """Render one fixed-width line placing each cell at its column start."""
    width = max(columns.values()) + 40
    buf = [" "] * width
    for name, start in columns.items():
        for offset, char in enumerate(cells.get(name, "")):
            buf[start + offset] = char
    return "".join(buf).rstrip()


def _page(
    columns: dict[str, int], header: dict[str, list[str]], rows: list[dict[str, str]]
) -> str:
    """Render a parts-list page whose header labels sit exactly over their
    column starts (so detection sees the same alignment poppler ``-layout``
    produces), stacking multi-word header labels across rows."""
    header_rows = max(len(v) for v in header.values())
    lines = [
        _row(
            columns,
            {name: parts[r] for name, parts in header.items() if r < len(parts)},
        )
        for r in range(header_rows)
    ]
    lines.extend(_row(columns, row) for row in rows)
    return "\n".join(lines)


def test_ehb_rotated_header_with_refdes_and_indent():
    """NWS/EHB IPBs print a tall rotated banner: REF DES after the index,
    CAGE after DESCRIPTION, an INDENT digit column, and a "figure-index"
    composite index ("2-57" = figure 2, index 57)."""
    columns = {
        "index": 0,
        "refdes": 13,
        "part": 30,
        "indent": 50,
        "desc": 54,
        "cage": 85,
        "units": 108,
        "smr": 116,
    }
    header = {
        "index": ["FIGURE AND", "INDEX", "NUMBER"],
        "refdes": ["", "REF DES"],
        "part": ["", "PART NUMBER"],
        "indent": ["INDENT"],
        "desc": ["", "DESCRIPTION"],
        "cage": ["", "", "CAGE", "CODE"],
        "units": ["QTY PER ASSY"],
        "smr": ["", "", "", "SMR"],
    }
    rows = [
        {
            "index": "2-57",
            "refdes": "1WG102",
            "part": "2237-A-00",
            "indent": "2",
            "desc": "FILTER, HARMONIC WAVE-",
            "cage": "28916",
            "units": "1",
            "smr": "PAOLD",
        },
        {"desc": "GUIDE, BANDPASS"},
        {
            "refdes": "MS16995",
            "part": "MS16995-51",
            "indent": "2",
            "desc": "SCREW, CAP, SOCKET HEAD",
            "cage": "96906",
            "units": "8",
            "smr": "PAOZZ",
        },
    ]
    entries, _ = parse_parts_lists([_page(columns, header, rows)], first_page_number=40)

    first = entries[0]
    assert first.figure_number_raw == "2"
    assert first.figure_index_raw == "57"
    assert first.reference_designator_raw == "1WG102"
    assert first.part_number_raw == "2237-A-00"
    assert first.indenture_level == 2
    assert first.cage_raw == "28916"
    assert first.smr_raw == "PAOLD"
    assert "FILTER, HARMONIC" in first.description_raw
    # Wrapped continuation folds into the preceding row, not a new entry.
    assert "BANDPASS" in first.description_raw
    assert entries[1].part_number_raw == "MS16995-51"


def test_cable_cross_reference_table_reads_nsn_column():
    """The NWS/EHB cable grid has an explicit NSN column and no DESCRIPTION;
    a LENGTH column between NSN and WHERE USED must not bleed into the NSN."""
    columns = {"index": 1, "refdes": 14, "part": 26, "nsn": 42, "length": 64, "uoc": 72}
    header = {
        "index": ["", "INDEX NO."],
        "refdes": ["", "REF DES"],
        "part": ["", "PART NO."],
        "nsn": ["", "NSN"],
        "length": ["LENGTH", "", "(IN)"],
        "uoc": ["", "WHERE USED"],
    }
    rows = [
        {
            "index": "1-62",
            "refdes": "W3-301",
            "part": "2320175-301",
            "nsn": "6150-01-360-9803",
            "length": "730",
            "uoc": "FSP",
        },
        {
            "refdes": "W3-302",
            "part": "2320175-302",
            "nsn": "6150-01-360-9804",
            "length": "927",
            "uoc": "LPP",
        },
    ]
    entries, _ = parse_parts_lists([_page(columns, header, rows)], first_page_number=1)

    assert entries[0].reference_designator_raw == "W3-301"
    assert entries[0].part_number_raw == "2320175-301"
    assert entries[0].nsn_raw == "6150-01-360-9803"
    assert entries[1].reference_designator_raw == "W3-302"
    assert entries[1].nsn_raw == "6150-01-360-9804"


def test_qty_header_aliases_units_per_assembly():
    """Some tables label the quantity column 'QTY PER ASSY' instead of
    'UNITS PER ASSY'."""
    columns = {"index": 3, "part": 14, "cage": 26, "desc": 36, "units": 60, "smr": 70}
    header = {
        "index": ["FIGURE &", "INDEX"],
        "part": ["PART", "NUMBER"],
        "cage": ["", "CAGE"],
        "desc": ["DESCRIPTION"],
        "units": ["QTY", "PER", "ASSY"],
        "smr": ["SMR", "CODE"],
    }
    rows = [
        {
            "index": "1/1",
            "part": "9619541",
            "cage": "6T656",
            "desc": "DISC SW NF 60A",
            "units": "1",
            "smr": "XB",
        }
    ]
    entries, _ = parse_parts_lists([_page(columns, header, rows)], first_page_number=1)

    assert entries[0].units_per_assembly_raw == "1"
    assert entries[0].smr_raw == "XB"
    assert "DISC SW" in entries[0].description_raw


def test_short_part_numbers_under_wide_header_snap_to_data():
    """Headers are centered over their data: a 5-char vendor part number under
    a wide PART NUMBER banner leaves a clean gutter on BOTH sides, and the
    boundary must snap to where the data starts, not the rightmost gutter
    (33D2-5-36-44 page 42 regression — part numbers were swallowed by the
    index cell)."""
    page = "\n".join(
        [
            " FIGURE &                                                    UNITS   USABLE",
            "   INDEX/          PART                                       PER      ON     SMR",
            " SHEET NO.        NUMBER   CAGE    1234567                   ASSY     CODE    CODE",
            "",
            " 10-         53M40         53964   LOW-PRESSURE FILTER ASSEMBLY                REF   AOO",
            "         1   53M47         53964   . COVER . . . . . . . . .                     1   PAOZZ",
            "         2   53M49         53964   . GASKET, Cover . . . . .                     1   PAOZZ",
        ]
    )
    entries, _ = parse_parts_lists([page], first_page_number=42)
    by_index = {e.figure_index_raw: e for e in entries}
    assert by_index["1"].part_number_raw == "53M47"
    assert by_index["2"].part_number_raw == "53M49"
    assert all(e.cage_raw == "53964" for e in entries)
    assert not any("missing part number" in e.validation_flags for e in entries)


def test_date_footer_is_not_a_parts_row():
    """A page-footer date ("20 FEBRUARY 2015") in the table body is skipped,
    not parsed as a row nor merged into the previous description."""
    columns = {"index": 3, "part": 14, "cage": 26, "desc": 36, "units": 60, "smr": 70}
    header = {
        "index": ["FIGURE &", "INDEX"],
        "part": ["PART", "NUMBER"],
        "cage": ["", "CAGE"],
        "desc": ["DESCRIPTION"],
        "units": ["UNITS", "PER", "ASSY"],
        "smr": ["SMR", "CODE"],
    }
    rows = [
        {
            "index": "1/1",
            "part": "9619541",
            "cage": "6T656",
            "desc": "HANDLE, ROTARY",
            "units": "1",
            "smr": "XB",
        },
    ]
    page = _page(columns, header, rows)
    page += "\n20 FEBRUARY 2015\n" + _row(
        columns,
        {
            "index": "2/1",
            "part": "9611279",
            "cage": "6T656",
            "desc": "LATCH, COMPRESSION",
            "units": "1",
            "smr": "XB",
        },
    )
    entries, _ = parse_parts_lists([page], first_page_number=1)

    assert len(entries) == 2
    assert entries[0].description_raw == "HANDLE, ROTARY"
    assert entries[1].description_raw == "LATCH, COMPRESSION"


def test_composite_index_keeps_sheet_suffix():
    """Composite "index/sheet" values are preserved verbatim in the entry
    (the figure-hotspot pass splits off the index for callout matching)."""
    columns = {"index": 3, "part": 14, "cage": 26, "desc": 36, "units": 60, "smr": 70}
    header = {
        "index": ["FIGURE &", "INDEX/", "SHEET NO."],
        "part": ["PART", "NUMBER"],
        "cage": ["", "CAGE"],
        "desc": ["DESCRIPTION"],
        "units": ["UNITS", "PER", "ASSY"],
        "smr": ["SMR", "CODE"],
    }
    rows = [
        {
            "index": "14/2",
            "part": "9619541",
            "cage": "6T656",
            "desc": "INDICATOR",
            "units": "1",
            "smr": "XB",
        }
    ]
    entries, _ = parse_parts_lists([_page(columns, header, rows)], first_page_number=1)

    assert entries[0].figure_index_raw == "14/2"


def test_table_of_contents_figure_references_are_not_rendered_figures():
    pages = [
        "\n".join(
            [
                "TABLE OF CONTENTS",
                "Figure 1. Motor Generator Set ................. 2-1",
                "Figure 2. Housing Assembly .................... 2-4",
            ]
        ),
        "Figure 1. Motor Generator Set",
        "Figure 2. Housing Assembly",
    ]

    _entries, figures = parse_parts_lists(pages)

    assert [(figure.figure_number, figure.page_number) for figure in figures] == [
        ("1", 2),
        ("2", 3),
    ]
