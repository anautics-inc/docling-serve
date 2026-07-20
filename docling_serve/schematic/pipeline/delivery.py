"""Delivery checks shared by schematic extraction and revision."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docling_serve.schematic.pipeline.rendering import (
    export_kicad_svg,
    kicad_cli_available,
)

KBL_XSD_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "tests"
    / "test_files"
    / "KBL24_SR1.xsd"
)


@dataclass
class CheckResult:
    """One delivery-check verdict."""

    id: str
    label: str
    status: str  # "pass" | "warn" | "fail" | "skip"
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
        }


def check_graph_integrity(graph: dict[str, Any]) -> list[CheckResult]:
    """The structural gate: is the extracted/edited graph internally sound?"""
    from docling_serve.schematic.artifacts import validate_artifact

    results: list[CheckResult] = []
    try:
        validate_artifact(graph, "schematic-graph.schema.json")
        results.append(CheckResult("schema", "Graph schema", "pass", "schema-valid"))
    except Exception as error:
        results.append(CheckResult("schema", "Graph schema", "fail", str(error)[:200]))

    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]
    known_ids = {str(c.get("id")) for c in components}

    unboxed = sum(1 for c in components if not c.get("bbox"))
    results.append(
        CheckResult(
            "components",
            "Components located",
            "pass" if unboxed == 0 else "warn",
            f"{len(components)} components, {unboxed} without a drawing location",
        )
    )

    dangling_unnamed = 0
    off_page = 0
    orphan_refs = 0
    with_geometry = 0
    for net in nets:
        nodes = [n for n in net.get("nodes") or [] if isinstance(n, dict)]
        touched = {str(n.get("component")) for n in nodes if n.get("component")}
        orphan_refs += sum(1 for c in touched if c not in known_ids)
        if len(touched) < 2:
            # A NAMED single-ended net is the drawing saying "continues
            # elsewhere" (+13 VDC OUTPUT, B+, …) — an off-page run, not a
            # defect this sheet can fix. Only unnamed single-ended nets are
            # suspicious (untraceable copper).
            if net.get("name"):
                off_page += 1
            else:
                dangling_unnamed += 1
        if net.get("segments"):
            with_geometry += 1
    connected = len(nets) - dangling_unnamed - off_page
    detail = f"{connected}/{len(nets)} nets join 2+ components"
    if off_page:
        detail += f", {off_page} named off-page run(s)"
    if dangling_unnamed:
        detail += f", {dangling_unnamed} unnamed single-ended net(s)"
    results.append(
        CheckResult(
            "connectivity",
            "Lines connected",
            "pass" if dangling_unnamed == 0 else "warn",
            detail,
        )
    )
    results.append(
        CheckResult(
            "references",
            "Net references resolve",
            "pass" if orphan_refs == 0 else "fail",
            f"{orphan_refs} net endpoint(s) reference unknown components",
        )
    )
    results.append(
        CheckResult(
            "geometry",
            "Nets carry wire geometry",
            "pass" if with_geometry else "warn",
            f"{with_geometry}/{len(nets)} nets have traced wire segments",
        )
    )
    return results


def check_kicad_opens(kicad_path: Path) -> CheckResult:
    """KiCad itself plots the document — if it plots, it opens."""
    if not kicad_cli_available():
        return CheckResult("kicad", "Opens in KiCad", "skip", "kicad-cli not installed")

    with tempfile.TemporaryDirectory() as out:
        result = export_kicad_svg(kicad_path, out)
    if result.returncode != 0:
        return CheckResult(
            "kicad", "Opens in KiCad", "fail", result.stderr.strip()[:200]
        )
    return CheckResult("kicad", "Opens in KiCad", "pass", "KiCad plotted the schematic")


def check_kicad_erc(kicad_path: Path) -> CheckResult:
    """KiCad electrical rule check with a per-type violation summary."""
    if not shutil.which("kicad-cli"):
        return CheckResult(
            "erc", "Electrical rule check", "skip", "kicad-cli not installed"
        )
    from docling_serve.schematic.kicad_symbols import ensure_kicad_cli_config

    ensure_kicad_cli_config()
    with tempfile.TemporaryDirectory() as out:
        report = Path(out) / "erc.rpt"
        result = subprocess.run(
            ["kicad-cli", "sch", "erc", "--output", str(report), str(kicad_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        text = report.read_text() if report.exists() else ""
    if result.returncode != 0 and not text:
        return CheckResult(
            "erc", "Electrical rule check", "fail", result.stderr.strip()[:200]
        )
    by_type: dict[str, int] = {}
    for match in re.finditer(r"\[([a-z_]+)\]", text):
        by_type[match.group(1)] = by_type.get(match.group(1), 0) + 1
    total = sum(by_type.values())
    summary = (
        ", ".join(f"{count}x {kind}" for kind, count in sorted(by_type.items()))
        if by_type
        else "no violations"
    )
    return CheckResult(
        "erc",
        "Electrical rule check",
        "pass" if total == 0 else "warn",
        summary[:300],
    )


def check_netlist(netlist_path: Path) -> CheckResult:
    text = netlist_path.read_text()
    if text.count("(") != text.count(")"):
        return CheckResult(
            "netlist", "KiCad netlist", "fail", "unbalanced S-expression"
        )
    if not text.lstrip().startswith("(export"):
        return CheckResult("netlist", "KiCad netlist", "fail", "not a netlist export")
    return CheckResult("netlist", "KiCad netlist", "pass", "S-expression balanced")


def check_kbl(kbl_path: Path) -> CheckResult:
    try:
        from lxml import etree
    except ImportError:
        return CheckResult("kbl", "KBL (EE Vision)", "skip", "lxml not installed")
    if not KBL_XSD_PATH.exists():
        return CheckResult("kbl", "KBL (EE Vision)", "skip", "KBL XSD not present")
    schema = etree.XMLSchema(etree.parse(str(KBL_XSD_PATH)))
    document = etree.parse(str(kbl_path))
    if schema.validate(document):
        return CheckResult(
            "kbl", "KBL (EE Vision)", "pass", "KBL 2.4 SR-1 schema-valid"
        )
    first = schema.error_log[0]
    return CheckResult(
        "kbl", "KBL (EE Vision)", "fail", f"line {first.line}: {first.message[:160]}"
    )


def check_xml(xml_path: Path) -> CheckResult:
    try:
        from lxml import etree

        etree.parse(str(xml_path))
        return CheckResult("xml", "XML export", "pass", "well-formed")
    except ImportError:
        return CheckResult("xml", "XML export", "skip", "lxml not installed")
    except Exception as error:
        return CheckResult("xml", "XML export", "fail", str(error)[:200])


def check_spice(spice_path: Path, graph: dict[str, Any] | None = None) -> CheckResult:
    """Solve the schematic's RUNNABLE deck through libngspice (PySpice).

    With the graph available, the gate builds the same auto-sourced deck the
    ``/simulate`` action solves (supplies from net names, grounds tied to
    node 0, rshunt against floating nodes) — so "SPICE: pass" means the model
    actually converges, not merely that a topology-only deck parses. Without
    a graph it falls back to bare elaboration of the file as-is.
    """
    from docling_serve.schematic.spice_simulation import (
        _ngspice_available,
        _run_pyspice,
        runnable_deck,
    )

    if not _ngspice_available():
        return CheckResult(
            "spice",
            "SPICE simulation netlist",
            "skip",
            "PySpice/libngspice not available",
        )
    deck = spice_path.read_text()
    notes = ""
    if graph is not None and "* simulation stimulus" not in deck:
        deck, info = runnable_deck(deck, graph)
        supplies = ", ".join(f"{s['name']} {s['volts']}V" for s in info["supplies"][:4])
        notes = f" (auto-sourced: {supplies or 'no supplies'})"
    elif ".op" not in deck.lower() and ".control" not in deck.lower():
        deck = (
            deck.replace(".end\n", ".op\n.end\n")
            if ".end\n" in deck
            else deck + "\n.op\n.end\n"
        )
    ok, voltages, _currents, log = _run_pyspice(deck, timeout_s=120.0)
    if not ok:
        return CheckResult(
            "spice", "SPICE simulation netlist", "fail", (log or "no solution")[:200]
        )
    return CheckResult(
        "spice",
        "SPICE simulation netlist",
        "pass",
        f"converged: {len(voltages)} node voltage(s){notes}"[:300],
    )


def run_delivery_checks(
    graph: dict[str, Any], schematic_dir: Path
) -> list[CheckResult]:
    """The full acceptance suite over a local copy of the schematic dir."""
    results = check_graph_integrity(graph)

    def first(pattern: str) -> Path | None:
        found = sorted(schematic_dir.glob(pattern))
        return found[0] if found else None

    for pattern, check in (
        ("*.kicad_sch", check_kicad_opens),
        ("*.kicad_sch", check_kicad_erc),
        ("*.net", check_netlist),
        ("*.kbl", check_kbl),
        ("*.xml", check_xml),
    ):
        path = first(pattern)
        if path is None:
            check_id = (
                check.__name__.replace("check_", "")
                .replace("kicad_opens", "kicad")
                .replace("kicad_erc", "erc")
            )
            results.append(
                CheckResult(check_id, pattern, "skip", "artifact not in bundle")
            )
            continue
        try:
            results.append(check(path))
        except Exception as error:  # pragma: no cover - tool-environment dependent
            name = check.__name__.replace("check_", "")
            results.append(CheckResult(name, pattern, "fail", str(error)[:200]))

    # SPICE gate: solve the structural netlist with auto-detected stimulus
    # (the graph makes it runnable — see check_spice). ``*.run.cir`` decks
    # already carry their stimulus, so the structural .cir is the input.
    spice_path = next(
        (
            p
            for p in sorted(schematic_dir.glob("*.cir"))
            if not p.name.endswith(".run.cir")
        ),
        None,
    )
    if spice_path is None:
        results.append(CheckResult("spice", "*.cir", "skip", "artifact not in bundle"))
    else:
        try:
            results.append(check_spice(spice_path, graph))
        except Exception as error:  # pragma: no cover - tool-environment dependent
            results.append(CheckResult("spice", "*.cir", "fail", str(error)[:200]))
    return results


def check_report_dict(
    checks: list[CheckResult], graph: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The persistable delivery-check report (manifest / deep-doc shape).

    Carries an explicit fidelity caveat: these verdicts prove the EXTRACTED
    model is internally coherent and tool-loadable — they cannot prove the
    drawing was read correctly. A converged SPICE run over phantom parts is
    false confidence, so the caveat quantifies how much of the component
    list carries drawing evidence.
    """
    from datetime import datetime, timezone

    report: dict[str, Any] = {
        "checks": [c.as_dict() for c in checks],
        "passed": all(c.status != "fail" for c in checks),
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    caveat = (
        "Verdicts validate the extracted model's coherence, not drawing "
        "fidelity — review components without printed identity against the "
        "original."
    )
    if graph is not None:
        quality = graph.get("connectivityQuality") or {}
        fraction = quality.get("verifiedComponentFraction")
        if isinstance(fraction, (int, float)):
            caveat += f" Evidence-backed components: {round(fraction * 100)}%."
        calibrated = graph.get("confidenceCalibrated")
        if isinstance(calibrated, (int, float)):
            report["confidenceCalibrated"] = calibrated
        # Surface the confidence gate on the report so the UI and any
        # downstream consumer can route a low-evidence extraction to review.
        if quality.get("needsReview"):
            report["needsReview"] = True
            caveat += " LOW CONFIDENCE - human review recommended before use."
    report["caveat"] = caveat
    return report


__all__ = [
    "CheckResult",
    "check_graph_integrity",
    "check_kbl",
    "check_kicad_erc",
    "check_kicad_opens",
    "check_netlist",
    "check_report_dict",
    "check_spice",
    "check_xml",
    "run_delivery_checks",
]
