import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from docling_serve.api.deps import ApiDependencies
from docling_serve.auth import AuthenticationResult

_log = logging.getLogger(__name__)


def create_schematic_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()
    require_auth = deps.require_auth

    def _schematic_bucket(body: dict) -> str:
        return (
            str(body.get("bucket") or "") or deps.settings.artifact_storage_bucket or ""
        ).strip()

    # CAD-style delivery check for a published schematic bundle (graph integrity,
    # KiCad open/ERC, netlist, KBL XSD, XML, ngspice). Body: {prefix, bucket?}.
    @router.post(
        "/v1/schematic/check",
        tags=["schematic"],
        summary="Run the CAD delivery checks on a published schematic bundle",
    )
    async def schematic_check(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: dict,
    ):
        """Run the CAD-style delivery checks (graph integrity, KiCad open/ERC,
        netlist, KBL XSD, XML, ngspice elaboration) on a published schematic bundle.

        Body: ``{prefix, bucket?}``. Returns ``{checks: [{name, status, detail}],
        passed}``; ``404`` when the bundle is missing.
        """
        from docling_serve.schematic.schematic_revision import check_schematic_bundle

        prefix = deps.validated_bundle_prefix(str(body.get("prefix") or ""))
        bucket = _schematic_bucket(body)
        if not (prefix and bucket):
            raise HTTPException(
                status_code=422,
                detail="prefix and a bucket (or configured artifact storage) are required.",
            )
        try:
            checks = check_schematic_bundle(bucket, prefix)
        except (FileNotFoundError, ValueError) as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        return {
            "checks": [c.as_dict() for c in checks],
            "passed": all(c.status != "fail" for c in checks),
        }

    # Apply browser edits to a schematic bundle and regenerate every derived artifact,
    # republish, and return the post-edit delivery check. Body: {prefix, bucket?, edits}.
    @router.post(
        "/v1/schematic/revise",
        tags=["schematic"],
        summary="Apply edits to a schematic bundle and regenerate every artifact",
    )
    async def schematic_revise(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: dict,
    ):
        """Apply component/net edits to a published schematic bundle, regenerate
        every derived artifact (KiCad, netlist, KBL, SPICE, XML, EDML), republish,
        and re-run the delivery checks.

        Body: ``{prefix, bucket?, edits: {components, nets}}`` (components/nets are
        addressed by graph id; ``delete: true`` drops a false detection). Returns
        ``{checks, passed, applied, notes}``; ``404`` when the bundle is missing.
        """
        from docling_serve.schematic.pipeline.regeneration import (
            revise_schematic_bundle,
        )

        prefix = deps.validated_bundle_prefix(str(body.get("prefix") or ""))
        bucket = _schematic_bucket(body)
        edits = body.get("edits")
        if not (prefix and bucket and isinstance(edits, dict)):
            raise HTTPException(
                status_code=422,
                detail="prefix, a bucket, and an edits object are required.",
            )
        try:
            outcome = revise_schematic_bundle(bucket, prefix, edits)
        except (FileNotFoundError, ValueError) as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        return {
            "checks": [c.as_dict() for c in outcome.checks],
            "passed": all(c.status != "fail" for c in outcome.checks),
            "applied": outcome.applied,
            "notes": outcome.notes,
        }

    # DC operating-point simulation of a published schematic (real ngspice solve).
    # Body: {prefix, bucket?, sources?:[{net, volts}]}.
    @router.post(
        "/v1/schematic/simulate",
        tags=["schematic"],
        summary="Run a real ngspice DC operating-point simulation on a schematic",
    )
    async def schematic_simulate(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: dict,
    ):
        """Run a REAL ngspice DC operating-point solve (in-process libngspice via
        PySpice) on a published schematic: energize specific nets or use the
        auto-detected supplies.

        Body: ``{prefix, bucket?, sources?: [{net, volts}]}``. Returns the circuit
        classification, what was energized, and the resulting node voltages.
        """
        import dataclasses
        import json as _json

        import boto3

        from docling_serve.schematic.spice_simulation import simulate_graph

        prefix = deps.validated_bundle_prefix(str(body.get("prefix") or ""))
        bucket = _schematic_bucket(body)
        if not (prefix and bucket):
            raise HTTPException(
                status_code=422, detail="prefix and a bucket are required."
            )
        client = boto3.client("s3")
        try:
            graph = _json.loads(
                client.get_object(
                    Bucket=bucket, Key=f"{prefix}/schematic/schematic-graph.json"
                )["Body"].read()
            )
        except Exception as err:
            raise HTTPException(
                status_code=404, detail=f"schematic graph not found: {err}"
            ) from err
        sources = body.get("sources") if isinstance(body.get("sources"), list) else []
        source = graph.get("source") or {}
        result = simulate_graph(
            graph,
            source_name=str(source.get("originalFileName") or "schematic.pdf"),
            sources=sources,
        )
        return {
            "ok": result.ok,
            "classification": dataclasses.asdict(result.classification),
            "supplies": result.supplies,
            "grounds": result.grounds,
            "nodeVoltages": result.nodeVoltages,
            "sourceCurrents": result.sourceCurrents,
            "warnings": result.warnings,
            "engine": result.engine,
        }

    return router
