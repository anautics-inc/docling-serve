"""Validate an extraction bundle's schematic artifacts against REAL tools.

Proves "will it open in their tool" without owning a license for every tool:

* ``.kicad_sch``  → opened and plotted by KiCad itself (``kicad-cli``); if
  KiCad can plot it, KiCad can open it.
* ``.net``        → parsed as an S-expression and re-exported netlist check
  via ``kicad-cli`` is skipped (netlist import is interactive), so the file
  is structurally parsed instead.
* ``.kbl``        → schema-validated against the OFFICIAL prostep/VDA
  KBL 2.4 SR-1 XSD — the conformance gate for Altair EE Vision, which
  converts KBL into its native EDB model.
* ``.xml``        → well-formedness.

Usage:
    python scripts/validate_schematic_artifacts.py <bundle_dir/schematic>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

XSD_PATH = Path(__file__).parent.parent / "tests" / "test_files" / "KBL24_SR1.xsd"


def check_kicad(path: Path) -> str:
    if not shutil.which("kicad-cli"):
        return "SKIP (kicad-cli not installed)"
    with tempfile.TemporaryDirectory() as out:
        result = subprocess.run(
            ["kicad-cli", "sch", "export", "svg", "--output", out, str(path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    if result.returncode != 0:
        return f"FAIL ({result.stderr.strip()[:200]})"
    return "PASS (KiCad plotted it)"


def check_kbl(path: Path) -> str:
    from lxml import etree

    schema = etree.XMLSchema(etree.parse(str(XSD_PATH)))
    document = etree.parse(str(path))
    if schema.validate(document):
        return "PASS (KBL 2.4 SR-1 schema-valid)"
    first = schema.error_log[0]
    return f"FAIL (line {first.line}: {first.message[:160]})"


def check_xml(path: Path) -> str:
    from lxml import etree

    etree.parse(str(path))
    return "PASS (well-formed)"


def check_spice(path: Path) -> str:
    if not shutil.which("ngspice"):
        return "SKIP (ngspice not installed)"
    checked = path.read_text().replace(
        ".end\n", ".control\nlisting e\nquit\n.endc\n.end\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".cir", delete=False) as handle:
        handle.write(checked)
        temp_path = handle.name
    try:
        result = subprocess.run(
            ["ngspice", "-b", temp_path], capture_output=True, text=True, timeout=120
        )
    finally:
        Path(temp_path).unlink(missing_ok=True)
    combined = (result.stdout + result.stderr).lower()
    if result.returncode != 0 or "error" in combined.replace("no error", ""):
        first = next(
            (line for line in combined.splitlines() if "error" in line), "unknown"
        )
        return f"FAIL ({first[:160]})"
    return "PASS (ngspice elaborated it)"


def check_netlist(path: Path) -> str:
    text = path.read_text()
    if text.count("(") != text.count(")"):
        return "FAIL (unbalanced S-expression)"
    if not text.lstrip().startswith("(export"):
        return "FAIL (not a KiCad netlist export)"
    return "PASS (S-expression balanced)"


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    checks = {
        "*.kicad_sch": check_kicad,
        "*.kbl": check_kbl,
        "*.xml": check_xml,
        "*.net": check_netlist,
        "*.cir": check_spice,
    }
    failures = 0
    for pattern, check in checks.items():
        for path in sorted(target.glob(pattern)):
            try:
                verdict = check(path)
            except Exception as error:
                verdict = f"FAIL ({error})"
            print(f"{path.name:42} {verdict}")
            failures += verdict.startswith("FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
