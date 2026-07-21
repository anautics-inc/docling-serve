from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from docling_serve.schematic.kicad_sch import svg_to_kicad_sch
from docling_serve.schematic.pipeline.delivery import (
    check_kicad_erc,
    check_kicad_opens,
)


@pytest.mark.integration
def test_real_kicad_opens_and_checks_generated_schematic(tmp_path: Path) -> None:
    if os.getenv("DOCLING_SERVE_RUN_KICAD_TESTS") != "1":
        pytest.skip("DOCLING_SERVE_RUN_KICAD_TESTS is not configured")
    assert shutil.which("kicad-cli"), "kicad-cli is required on the KiCad runner"
    schematic = tmp_path / "acceptance.kicad_sch"
    schematic.write_text(
        svg_to_kicad_sch(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
            '<line x1="10" y1="10" x2="90" y2="90" stroke="black"/>'
            "</svg>",
            title="Docling KiCad acceptance",
        ),
        encoding="utf-8",
    )

    opened = check_kicad_opens(schematic)
    erc = check_kicad_erc(schematic)

    assert opened.status == "pass", opened
    assert erc.status in {"pass", "warn"}, erc
