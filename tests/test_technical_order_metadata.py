"""Title-page metadata parsing across the AF/NWS title-page variants.

These pin the parser against the layouts observed in the post-2014 IPB corpus
(merged publications, work packages, adopted-commercial cover sheets, NWS EHB
manuals) without committing the large real PDFs — the parser reads
``pages: list[str]`` so representative page text is enough.
"""

from __future__ import annotations

from docling_serve.technical_order.metadata import parse_to_metadata


def test_issue_date_shares_footer_line_with_change_block():
    """Merged publications print the issue date and the change block on one
    footer line ("25 OCTOBER 2012        CHANGE 3 - 20 MAY 2015")."""
    page = "\n".join(
        [
            "TO 33D2-5-36-44",
            "TECHNICAL MANUAL",
            "ILLUSTRATED PARTS BREAKDOWN",
            "PORTABLE HYDRAULIC TEST STAND",
            "TTU-228/E-1B",
            "PN 9780-0073",
            "NSN 4920-01-044-5927",
            "ACL-FILCO",
            "F41608-93-D-0526",
            "Published Under Authority of the Secretary of the Air Force",
            "25 OCTOBER 2012                          CHANGE 3 - 20 MAY 2015",
        ]
    )
    meta = parse_to_metadata([page], filename="33D2-5-36-44.pdf")

    assert meta.publication_date == "25 OCTOBER 2012"
    assert meta.change_level == "3"
    assert meta.change_date == "20 MAY 2015"
    assert meta.document_title == "PORTABLE HYDRAULIC TEST STAND TTU-228/E-1B"
    # Bare contract under the manufacturer line, manufacturer recovered above it.
    assert meta.contract_numbers_raw == "F41608-93-D-0526"
    assert meta.manufacturer_name_raw == "ACL-FILCO"


def test_wrapped_headline_does_not_swallow_nomenclature():
    """A nomenclature line that shares its row with the tail of the wrapped
    manual-type headline ("(MCFTM) WITH ILLUSTRATED PARTS BREAKDOWN") keeps its
    nomenclature prefix."""
    page = "\n".join(
        [
            "TO 35D3-40-3-1",
            "TECHNICAL MANUAL",
            "WORK PACKAGE",
            "OPERATIONS AND MAINTENANCE",
            "MULTI-USE, CHAFF AND FLARE TRANSPORT MODULE",
            "(MCFTM) WITH ILLUSTRATED PARTS BREAKDOWN",
            "PN 20122185",
            "NSN 1730-01-626-0679 RN",
            "Manual Prepared by Tyonek Manufacturing Group",
            "FA8518-14-C-0006",
            "8 APRIL 2022",
        ]
    )
    meta = parse_to_metadata([page], filename="35D3-40-3-1.pdf")

    assert meta.document_title == "MULTI-USE, CHAFF AND FLARE TRANSPORT MODULE (MCFTM)"
    assert meta.publication_date == "8 APRIL 2022"
    assert meta.manufacturer_name_raw == "Tyonek Manufacturing Group"
    assert meta.contract_numbers_raw == "FA8518-14-C-0006"


def test_maintenance_level_and_models_are_separated_from_title():
    """Maintenance-level qualifiers and MODEL lines are metadata, not part of
    the item nomenclature."""
    page = "\n".join(
        [
            "TO 35E9-314-4",
            "TECHNICAL MANUAL",
            "ILLUSTRATED PARTS BREAKDOWN",
            "ORGANIZATIONAL AND INTERMEDIATE MAINTENANCE",
            "FIELD DEPLOYABLE",
            "ENVIRONMENTAL CONTROL UNIT",
            "MODEL FDECU-2, FDECU-3, FDECU-4, FDECU-5 AND",
            "FDECU-9",
            "NSN 4120-01-449-0459",
            "11 APRIL 2014",
        ]
    )
    meta = parse_to_metadata([page], filename="35E9-314-4.pdf")

    assert meta.document_title == "FIELD DEPLOYABLE ENVIRONMENTAL CONTROL UNIT"
    assert meta.maintenance_level == "ORGANIZATIONAL AND INTERMEDIATE MAINTENANCE"
    assert "FDECU-2" in meta.models_raw and "FDECU-9" in meta.models_raw


def test_adopted_commercial_cover_sheet_labeled_fields():
    """The 'IDENTIFYING TECHNICAL PUBLICATION SHEET' layout stores identity in
    labeled ``FIELD: value`` lines rather than a nomenclature block."""
    page = "\n".join(
        [
            "TO 35E10-38-1",
            "IDENTIFYING TECHNICAL PUBLICATION SHEET",
            "MANUFACTURER: REFTEC INTERNATIONAL SYSTEMS LLC",
            "CONTRACT NUMBER: FA8540-12-C-0025",
            "EQUIPMENT: INDOOR LIQUID CHILLER",
            "MODEL NUMBERS: EQ2A03",
            "2 AUGUST 2019",
        ]
    )
    meta = parse_to_metadata([page], filename="35E10-38-1.pdf")

    assert meta.document_title == "INDOOR LIQUID CHILLER"
    assert meta.models_raw == "EQ2A03"
    assert meta.manufacturer_name_raw == "REFTEC INTERNATIONAL SYSTEMS LLC"
    assert meta.contract_numbers_raw == "FA8540-12-C-0025"
    assert meta.publication_date == "2 AUGUST 2019"


def test_side_banner_word_repeats_are_collapsed_in_title():
    """pdftotext merges rotated side-banner text onto the nomenclature lines,
    duplicating the model designator ("WSR-88D WSR-88D")."""
    page = "\n".join(
        [
            "TECHNICAL MANUAL",
            "ILLUSTRATED PARTS BREAKDOWN",
            "DOPPLER METEOROLOGICAL RADAR",
            "WSR-88D WSR-88D",
            "15 MARCH 2024",
        ]
    )
    meta = parse_to_metadata([page], filename="31P1-4-108-4.pdf")

    assert meta.document_title == "DOPPLER METEOROLOGICAL RADAR WSR-88D"
