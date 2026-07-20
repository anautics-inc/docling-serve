"""Browser-driven schematic revision + CAD-style delivery checks.

Mirrors the CAD "model check before delivery acceptance" workflow for
schematics: a user edits the extracted schematic in the workbench (rename a
component, fix a part number, correct a net name, delete a false detection),
the bundle's artifacts regenerate from the revised graph, and a CHECK suite
proves the deliverable against real tools:

* graph integrity   — schema-valid, components boxed, nets connected
* KiCad             — kicad-cli plots the ``.kicad_sch`` (opens in the tool)
* ERC               — kicad-cli electrical rule check, violations by type
* netlist           — S-expression balanced ``.net``
* KBL               — official VDA KBL 2.4 SR-1 XSD conformance (EE Vision)
* XML               — well-formedness
* SPICE             — ngspice elaborates the ``.cir``

Everything operates on the published S3 bundle (the durable source of
truth), so edits survive sessions and every consumer (viewer, downloads,
context-graph projection) sees one consistent revision.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docling_serve.schematic.pipeline.delivery import (
    CheckResult as CheckResult,
    check_graph_integrity as check_graph_integrity,
    check_kbl as check_kbl,
    check_kicad_erc as check_kicad_erc,
    check_kicad_opens as check_kicad_opens,
    check_netlist as check_netlist,
    check_report_dict as check_report_dict,
    check_spice as check_spice,
    check_xml as check_xml,
    run_delivery_checks as run_delivery_checks,
)

_log = logging.getLogger(__name__)

#: Editable component fields (whitelist — ids/geometry stay extractor-owned).
EDITABLE_COMPONENT_FIELDS = ("refDes", "partNumber", "value", "type", "description")
#: Editable net fields.
EDITABLE_NET_FIELDS = ("name",)


@dataclass
class RevisionOutcome:
    """What a revise call changed and how the revised bundle checks out."""

    applied: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Graph edits                                                                  #
# --------------------------------------------------------------------------- #


def apply_graph_edits(graph: dict[str, Any], edits: dict[str, Any]) -> dict[str, int]:
    """Fold user edits into the schematic graph, in place.

    Edit payload shape (all parts optional)::

        {"components": [{"id": "C0004", "refDes": "K2", "partNumber": "...",
                         "value": "...", "type": "...", "delete": true}],
         "nets":       [{"id": "N001-0003", "name": "A8B22", "delete": true}]}

    Components/nets are addressed by their stable extractor ids. Deleting a
    component also removes its net memberships (the net survives — its other
    endpoints are still real). Returns counters of what was applied.
    """
    components = [c for c in graph.get("components") or [] if isinstance(c, dict)]
    nets = [n for n in graph.get("nets") or [] if isinstance(n, dict)]

    edited_c, deleted_components = _apply_row_edits(
        components, edits.get("components"), EDITABLE_COMPONENT_FIELDS
    )
    edited_n, deleted_nets = _apply_row_edits(
        nets, edits.get("nets"), EDITABLE_NET_FIELDS
    )

    if deleted_components:
        components = [
            c for c in components if str(c.get("id")) not in deleted_components
        ]
        for net in nets:
            net["nodes"] = [
                node
                for node in net.get("nodes") or []
                if not (
                    isinstance(node, dict)
                    and str(node.get("component")) in deleted_components
                )
            ]
    if deleted_nets:
        nets = [n for n in nets if str(n.get("id")) not in deleted_nets]
    graph["components"] = components
    graph["nets"] = nets

    graph["revision"] = int(graph.get("revision") or 0) + 1
    return {
        "componentEdits": edited_c,
        "componentDeletes": len(deleted_components),
        "netEdits": edited_n,
        "netDeletes": len(deleted_nets),
    }


def _apply_row_edits(
    rows: list[dict[str, Any]],
    row_edits: Any,
    editable_fields: tuple[str, ...],
) -> tuple[int, set[str]]:
    """Field updates + delete markers for one row family (components or nets)."""
    by_id = {str(row.get("id")): row for row in rows}
    edited = 0
    deleted: set[str] = set()
    for edit in row_edits or []:
        if not isinstance(edit, dict):
            continue
        row = by_id.get(str(edit.get("id")))
        if row is None:
            continue
        if edit.get("delete"):
            deleted.add(str(row.get("id")))
            continue
        changed = False
        for field_name in editable_fields:
            if field_name in edit:
                value = edit[field_name]
                row[field_name] = str(value).strip() if value is not None else None
                changed = True
        edited += int(changed)
    return edited, deleted


# --------------------------------------------------------------------------- #
# Delivery checks                                                              #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Bundle download / upload                                                     #
# --------------------------------------------------------------------------- #


def _s3_client():
    import boto3

    return boto3.client("s3")


def _download_schematic_dir(
    bucket: str, prefix: str, target: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pull extraction.json + every schematic artifact into ``target``.

    Returns (manifest, graph).
    """
    client = _s3_client()
    manifest_raw = client.get_object(Bucket=bucket, Key=f"{prefix}/extraction.json")[
        "Body"
    ].read()
    manifest = json.loads(manifest_raw)
    if manifest.get("domain") != "schematic":
        raise ValueError("bundle is not a schematic extraction")

    paginator = client.get_paginator("list_objects_v2")
    schematic_prefix = f"{prefix}/schematic/"
    resolved_target = target.resolve()
    for page in paginator.paginate(Bucket=bucket, Prefix=schematic_prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            relative = key[len(schematic_prefix) :]
            if not relative:
                continue
            local = target / relative
            # S3 keys may carry path-like segments; a key resolving outside
            # the download directory is a traversal attempt, never a bundle
            # artifact.
            if not local.resolve().is_relative_to(resolved_target):
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(local))

    graph_path = target / "schematic-graph.json"
    if not graph_path.exists():
        raise FileNotFoundError("schematic-graph.json not in bundle")
    graph = json.loads(graph_path.read_text())
    return manifest, graph


def _upload_schematic_dir(bucket: str, prefix: str, source: Path) -> int:
    from docling_serve.storage import content_type_for

    client = _s3_client()
    uploaded = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        key = f"{prefix}/schematic/{path.relative_to(source).as_posix()}"
        client.upload_file(
            str(path), bucket, key, ExtraArgs={"ContentType": content_type_for(path)}
        )
        uploaded += 1
    return uploaded


def _update_manifest_counts(
    bucket: str,
    prefix: str,
    graph: dict[str, Any],
    kbl_check: CheckResult | None = None,
    source_stem: str | None = None,
    edb_written: bool = False,
    check_report: dict[str, Any] | None = None,
    kicad_pro_rel: str | None = None,
) -> None:
    client = _s3_client()
    key = f"{prefix}/extraction.json"
    manifest = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
    schematic = manifest.get("schematic")
    if isinstance(schematic, dict):
        schematic["componentCount"] = len(graph.get("components") or [])
        schematic["netCount"] = len(graph.get("nets") or [])
        schematic["revision"] = graph.get("revision")
        if source_stem and not schematic.get("eevisionCsv"):
            # Bundles extracted before the EEvision table existed gain the
            # manifest pointer when the revise regenerates artifacts.
            schematic["eevisionCsv"] = f"schematic/{source_stem}.eevision.csv"
        if source_stem:
            # The auto-sourced runnable deck regenerates on every revise.
            schematic["spiceRunnable"] = f"schematic/{source_stem}.run.cir"
        if kicad_pro_rel:
            # The KiCad project (ERC policy) belongs WITH the .kicad_sch —
            # downloading both gives the user our severities on open.
            schematic["kicadPro"] = kicad_pro_rel
        if edb_written and source_stem:
            # Native EEvision database — pointer appears once the vendor
            # EDB Creator API produced the file (extract-time or revise-time).
            schematic["edb"] = f"schematic/{source_stem}.edb"
        if check_report is not None:
            # The persisted delivery-check report: a revise must refresh it,
            # or the manifest would keep advertising a stale verdict.
            schematic["check"] = check_report
        if kbl_check is not None:
            # Formal standard targeting + validation evidence, recorded ON the
            # bundle (audit finding: "no schema authority binding at export").
            schematic["kblStandard"] = "VDA KBL 2.4 SR-1"
            schematic["kblSchemaValid"] = kbl_check.status == "pass"
            schematic["kblValidationDetail"] = kbl_check.detail
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )


# --------------------------------------------------------------------------- #
# Public entry points (used by the API endpoints)                              #
# --------------------------------------------------------------------------- #


def check_schematic_bundle(bucket: str, prefix: str) -> list[CheckResult]:
    """Run the delivery-check suite against a published bundle."""
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir)
        _, graph = _download_schematic_dir(bucket, prefix.strip("/"), target)
        return run_delivery_checks(graph, target)


def revise_schematic_bundle(
    bucket: str, prefix: str, edits: dict[str, Any]
) -> RevisionOutcome:
    """Apply edits, regenerate every derived artifact, republish, and re-check.

    The page SVGs (lossless geometry) and the original graph's traced
    segments are untouched by metadata edits, so geometry-replay artifacts
    stay valid; everything graph-derived (netlist, EDML, XML, KBL, SPICE,
    KiCad electrical objects + annotations, KiCad render previews) is
    rebuilt from the revised graph.
    """
    from docling_serve.schematic.edml import graph_to_edml
    from docling_serve.schematic.kbl import graph_to_kbl
    from docling_serve.schematic.kicad_sch import (
        KicadConversionError,
        svg_to_kicad_sch,
    )
    from docling_serve.schematic.netlist import graph_to_kicad_netlist
    from docling_serve.schematic.pipeline.rendering import (
        inject_net_wires,
        render_kicad_previews,
    )
    from docling_serve.schematic.spice import graph_to_spice
    from docling_serve.schematic.xml_export import graph_to_xml

    outcome = RevisionOutcome()
    prefix = prefix.strip("/")
    with tempfile.TemporaryDirectory() as workdir:
        target = Path(workdir)
        _, graph = _download_schematic_dir(bucket, prefix, target)

        outcome.applied = apply_graph_edits(graph, edits)
        # Bundles extracted before connectivity-id assignment existed gain
        # wire ids + 2-terminal pins on their next save (idempotent).
        from docling_serve.schematic.connectivity_ids import (
            assign_two_terminal_pins,
            assign_wire_ids,
            drop_quantity_annotations,
            drop_value_text_echoes,
            mark_ground_nets,
            merge_duplicate_detections,
            reattach_floating_components,
        )

        for tag, pass_notes in (
            ("dedup", merge_duplicate_detections(graph)),
            ("quantity_cleanup", drop_quantity_annotations(graph)),
            ("echo_cleanup", drop_value_text_echoes(graph)),
            ("reattach", reattach_floating_components(graph)),
        ):
            if pass_notes:
                outcome.notes.append(f"{tag}: {'; '.join(pass_notes[:10])}")
        grounded = mark_ground_nets(graph)
        if grounded:
            outcome.notes.append(f"ground_classes: {grounded} net(s) from glyphs")

        wire_ids = assign_wire_ids(graph)
        pins = assign_two_terminal_pins(graph)
        if wire_ids or pins:
            outcome.notes.append(
                f"connectivity_ids: {wire_ids} wire id(s), {pins} pin(s) assigned"
            )
        # Refresh the QA block: engineer edits (pin fills, deletions) move
        # memberships off the worklist on every save.
        from docling_serve.schematic.connectivity_ids import (
            record_connectivity_quality,
        )

        quality = record_connectivity_quality(graph)
        outcome.notes.append(
            f"connectivity_quality: {quality['pinnedCount']}/"
            f"{quality['membershipCount']} pinned"
        )
        source = graph.get("source") or {}
        # Older bundles predate the tenant stamp; the prefix is authoritative
        # (tenants/<t>/…). Tenant scoping drives model-library resolution.
        if not source.get("tenantId"):
            match = re.search(r"(?:^|/)tenants/([^/]+)/", prefix)
            if match:
                source["tenantId"] = match.group(1)
                graph["source"] = source
        source_name = str(
            source.get("originalFileName")
            or source.get("fileName")
            or next(iter(sorted(target.glob("*.net"))), Path("schematic.net")).name
        )
        stem = Path(source_name).stem

        (target / "schematic-graph.json").write_text(json.dumps(graph, indent=2))
        (target / f"{stem}.net").write_text(
            graph_to_kicad_netlist(graph, source_name=source_name)
        )
        (target / f"{stem}.edml").write_text(
            graph_to_edml(graph, source_name=source_name)
        )
        from docling_serve.schematic.eevision import graph_to_eevision_csv

        (target / f"{stem}.eevision.csv").write_text(
            graph_to_eevision_csv(graph, source_name=source_name)
        )
        (target / f"{stem}.xml").write_text(
            graph_to_xml(graph, source_name=source_name)
        )
        (target / f"{stem}.kbl").write_text(
            graph_to_kbl(graph, source_name=source_name)
        )
        spice_text = graph_to_spice(graph, source_name=source_name)
        (target / f"{stem}.cir").write_text(spice_text)
        from docling_serve.schematic.spice_simulation import runnable_deck

        run_deck, run_info = runnable_deck(spice_text, graph)
        (target / f"{stem}.run.cir").write_text(run_deck)
        outcome.notes.extend(f"spice_runnable: {w}" for w in run_info["warnings"])
        # Native EEvision database — optional (vendor EDB Creator API).
        edb_written = False
        try:
            from docling_serve.schematic.edb import graph_to_edb

            graph_to_edb(graph, target / f"{stem}.edb", source_name=source_name)
            edb_written = True
        except Exception as edb_error:
            outcome.notes.append(f"edb: unavailable ({edb_error})")

        # Rebuild KiCad documents so labels/annotations reflect the edits:
        # vector pages replay from the bundle's lossless SVG; scanned pages
        # keep their existing raster-backdrop document minus stale electrical
        # items (regenerating from the SVG would lose the bitmap).
        kicad_paths = sorted(target.glob("*.kicad_sch"))
        svg_pages = sorted(
            p for p in target.glob("*.svg") if not p.name.startswith("kicad-render")
        )
        rebuilt: list[Path] = []
        for index, kicad_path in enumerate(kicad_paths):
            existing = kicad_path.read_text()
            if "(image " in existing:
                kicad_path.write_text(_strip_injected_items(existing))
                rebuilt.append(kicad_path)
                continue
            if index < len(svg_pages):
                try:
                    kicad_path.write_text(
                        svg_to_kicad_sch(svg_pages[index].read_text(), title=stem)
                    )
                    rebuilt.append(kicad_path)
                    continue
                except (KicadConversionError, OSError) as error:
                    outcome.notes.append(f"kicad replay failed: {error}")
            kicad_path.write_text(_strip_injected_items(existing))
            rebuilt.append(kicad_path)
        inject_net_wires(rebuilt, graph, outcome.notes)
        from docling_serve.schematic.kicad_sch import write_project_files

        pro_paths = write_project_files(rebuilt)
        kicad_pro_rel = f"schematic/{pro_paths[0].name}" if pro_paths else None
        render_kicad_previews(rebuilt, target, notes=outcome.notes)

        uploaded = _upload_schematic_dir(bucket, prefix, target)
        outcome.notes.append(f"republished {uploaded} artifact(s)")

        outcome.checks = run_delivery_checks(graph, target)
        kbl_check = next((c for c in outcome.checks if c.id == "kbl"), None)
        _update_manifest_counts(
            bucket,
            prefix,
            graph,
            kbl_check=kbl_check,
            source_stem=stem,
            edb_written=edb_written,
            check_report=check_report_dict(outcome.checks, graph),
            kicad_pro_rel=kicad_pro_rel,
        )
    return outcome


#: Electrical/semantic items the injector adds (everything we must strip
#: before re-injecting from a revised graph).
_INJECTED_ITEM_RE = re.compile(
    r"^  \((?:wire |junction |label |global_label |text |symbol |rectangle |no_connect )",
    re.MULTILINE,
)


def _strip_injected_items(kicad_text: str) -> str:
    """Remove previously injected electrical/annotation items from a document.

    Top-level injected items start at exactly two spaces of indentation;
    their bodies are deeper-indented. Geometry replay items (polyline,
    image, lib_symbols, title_block, …) are preserved.
    """
    lines = kicad_text.splitlines()
    output: list[str] = []
    skipping = False
    depth = 0
    for line in lines:
        if not skipping and _INJECTED_ITEM_RE.match(line):
            skipping = True
            depth = line.count("(") - line.count(")")
            if depth <= 0:
                skipping = False
            continue
        if skipping:
            depth += line.count("(") - line.count(")")
            if depth <= 0:
                skipping = False
            continue
        output.append(line)
    return "\n".join(output) + ("\n" if kicad_text.endswith("\n") else "")
