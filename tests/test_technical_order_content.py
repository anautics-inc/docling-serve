"""Full-document content extraction (captify.to.v2)."""

from __future__ import annotations

from docling_serve.technical_order.content import parse_content

_TITLE = """\
                        TO 1X-2-3
                     TECHNICAL MANUAL
              SOME EQUIPMENT NOMENCLATURE
                       PN 12345
"""

_TOC = """\
                    TABLE OF CONTENTS
Chapter                                                        Page

1    INTRODUCTION . . . . . . . . . . . . . . . . . . . . . .   1-1
    1.1     DESCRIPTION . . . . . . . . . . . . . . . . . . .   1-1
"""

_PROSE = """\
                     CHAPTER 1
              INTRODUCTION AND GENERAL INFORMATION
1.1   DESCRIPTION.

1.1.1 General. The equipment consists of a housing and a con-
trol unit mounted on a common base.

                          WARNING

Disconnect input power before removing any access cover.

1.1.2 Controls. All operating controls are located on the front
panel.
"""


def test_page_kinds_and_block_types():
    result = parse_content(
        [_TITLE, _TOC, _PROSE, "", "figure page text", "parts page text"],
        figure_pages={
            5: {
                "figureNumber": "1-1",
                "figureTitle": "Housing",
                "mediaKey": "media/f.png",
            }
        },
        parts_pages={6},
    )
    kinds = [p["kind"] for p in result["pages"]]
    assert kinds == ["title-page", "toc", "prose", "blank", "figure", "parts-list"]
    assert result["schema"] == "captify.to.v2"
    assert result["compatibleSchemas"] == ["captify.to-content.v1"]


def test_prose_blocks_capture_structure():
    result = parse_content([_TITLE, _PROSE])
    blocks = result["pages"][1]["blocks"]
    types = [(b["type"], b.get("number", "")) for b in blocks]

    assert ("heading", "1") in types  # CHAPTER 1 + its title line
    assert ("heading", "1.1") in types  # numbered section heading
    para = next(b for b in blocks if b.get("number") == "1.1.1")
    # Hyphenated wrap rejoined: "con-\ntrol" -> "control".
    assert "control unit" in para["text"]
    adm = next(b for b in blocks if b["type"] == "admonition")
    assert adm["kind"] == "warning"
    assert adm["text"].startswith("Disconnect input power")
    assert any(b.get("number") == "1.1.2" for b in blocks)


def test_toc_entries_parse_number_title_page():
    result = parse_content([_TITLE, _TOC])
    toc = result["pages"][1]["blocks"]
    assert any(
        {
            "type": entry["type"],
            "number": entry["number"],
            "text": entry["text"],
            "page": entry["page"],
        }
        == {"type": "toc-entry", "number": "1", "text": "INTRODUCTION", "page": "1-1"}
        for entry in toc
    )
    assert any(
        {
            "type": entry["type"],
            "number": entry["number"],
            "text": entry["text"],
            "page": entry["page"],
        }
        == {
            "type": "toc-entry",
            "number": "1.1",
            "text": "DESCRIPTION",
            "page": "1-1",
        }
        for entry in toc
    )


def test_every_page_is_accounted_for():
    result = parse_content([_TITLE, _TOC, _PROSE])
    assert result["pageCount"] == 3
    assert sum(result["pageKinds"].values()) == 3


_LINKED_PROSE = """\
1.2   REMOVAL.

1.2.1 Fan. Remove the fan (Figure 1-1, 7) by removing screw
MS90725-64 and lockwasher 33637-41. Refer to paragraph 1.1.1
for handling. Serial 99999 does not apply.
"""


def _linked_result():
    return parse_content(
        [_TITLE, _PROSE, _LINKED_PROSE],
        figure_numbers={"1-1"},
        part_entries=[
            {"partNumber": "MS90725-64", "sequence": 11},
            {"partNumber": "33637-41", "sequence": 27},
        ],
    )


def test_links_ground_figure_part_and_paragraph_refs():
    result = _linked_result()
    para = next(b for b in result["pages"][2]["blocks"] if b.get("number") == "1.2.1")
    links = para["links"]
    by_type = {link["type"]: link for link in links}

    fig = by_type["figure"]
    assert fig["target"] == "1-1"
    assert fig["callout"] == "7"
    assert para["text"][fig["start"] : fig["end"]].startswith("Figure 1-1")

    assert by_type["paragraph"]["target"] == "1.1.1"

    part_targets = {link["target"] for link in links if link["type"] == "part"}
    assert part_targets == {"MS90725-64", "33637-41"}
    part = next(link for link in links if link["target"] == "MS90725-64")
    assert part["sequence"] == 11
    assert result["linkCount"] == len(links)


def test_ungrounded_tokens_do_not_link():
    result = _linked_result()
    para = next(b for b in result["pages"][2]["blocks"] if b.get("number") == "1.2.1")
    # "99999" is part-number-shaped but absent from the parts list.
    spans = [para["text"][link["start"] : link["end"]] for link in para["links"]]
    assert all("99999" not in s for s in spans)


def test_no_context_means_no_links():
    result = parse_content([_TITLE, _LINKED_PROSE])
    para = next(
        b for p in result["pages"] for b in p["blocks"] if b.get("number") == "1.2.1"
    )
    assert "links" not in para
    assert result["linkCount"] == 0
