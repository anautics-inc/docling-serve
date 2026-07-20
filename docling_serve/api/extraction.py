import json
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    UploadFile,
)
from starlette.concurrency import run_in_threadpool

from docling_serve.api.deps import ApiDependencies
from docling_serve.artifacts import publish_dir_to_s3
from docling_serve.auth import AuthenticationResult
from docling_serve.graph import (
    GraphExtractionUnavailable,
    GraphExtractRequest,
    GraphExtractResponse,
    graph_payload_from_text,
    resolve_profile_template,
)

_log = logging.getLogger(__name__)


def create_extraction_router(deps: ApiDependencies) -> APIRouter:  # noqa: C901
    router = APIRouter()
    require_auth = deps.require_auth

    async def execute_typed_domain(
        upload: UploadFile,
        *,
        tenant_id: str,
        domain: str,
        fallback_name: str,
        work_prefix: str,
        prefix: str,
        bucket: str,
        execute: Callable[[Path, Path, str, str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        prefix = deps.validated_bundle_prefix(prefix)
        target_bucket = (bucket or deps.settings.artifact_storage_bucket or "").strip()
        work = Path(tempfile.mkdtemp(prefix=work_prefix))
        try:
            async with deps.admit_typed_upload(
                upload,
                tenant_id=tenant_id,
                domain=domain,
                fallback_name=fallback_name,
            ) as admitted:
                return await execute(
                    admitted.path, work / "bundle", prefix, target_bucket
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def published_result(
        result: dict[str, Any],
        bundle: Path,
        prefix: str,
        bucket: str,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        published = (
            publish_dir_to_s3(bundle, bucket=bucket, prefix=prefix)
            if enabled and prefix and bucket
            else []
        )
        return {
            **result,
            "s3Keys": published,
            "bucket": bucket if published else None,
            "prefix": prefix if published else None,
        }

    # Knowledge-graph extraction (NER replacement) over already-converted text.
    # Conversion is docling's OOTB job; this only runs docling-graph on the text
    # the caller already produced. Body: {text, template?, profile?}. Returns
    # {nodes, edges, labels, edgeLabels, nodeCount, edgeCount, ...}; an empty graph
    # plus a `note` when graph extraction is unconfigured/unavailable, so callers
    # (pytology) degrade uniformly instead of erroring.
    @router.post(
        "/v1/graph/extract",
        tags=["graph"],
        response_model=GraphExtractResponse,
        summary="Extract a knowledge graph (entities + relations) from converted text",
    )
    def extract_graph(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: GraphExtractRequest,
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ) -> GraphExtractResponse:
        """Run docling-graph entity/relation extraction over already-converted text
        (the NER replacement) — conversion itself is docling's OOTB job.

        Body: ``{text, template?, profile?}`` (``profile`` selects a built-in
        template, e.g. ``schematic``/``access``; ``template`` is a dotted import
        path). Returns ``{nodes, edges, labels, edgeLabels, nodeCount, edgeCount}``,
        or an empty graph + a ``note`` when extraction is unconfigured so callers
        degrade uniformly.
        """
        template = body.template or resolve_profile_template(body.profile)
        if not deps.settings.graph_extraction_enabled:
            return GraphExtractResponse(
                note="Graph extraction is disabled by service policy."
            )
        # Forward the tenant to the proxy as a spend tag (the proxy key is
        # service-scoped, so this is how graph-extraction spend is attributed).
        tenant_id = deps.get_tenant_id(x_tenant_id)
        identity_headers = {"x-tenant-id": tenant_id} if tenant_id else None
        try:
            payload = graph_payload_from_text(
                body.text, template=template, identity_headers=identity_headers
            )
        except GraphExtractionUnavailable as err:
            _log.info("Graph extraction unavailable: %s", err)
            return GraphExtractResponse(note=str(err))
        return GraphExtractResponse(**payload)

    # Microsoft Access (.mdb/.accdb) — docling has no Access backend, so this gap is
    # filled by converting the database to docling-native markdown tables (access-parser),
    # which chunking + graph extraction then consume out of the box.
    @router.post(
        "/v1/extract/access",
        tags=["extract"],
        summary="Convert a Microsoft Access database to docling-native markdown tables",
    )
    async def extract_access(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        """Convert an uploaded Access database (.mdb/.accdb) into docling-native
        GitHub-flavored markdown — one section + table per Access table — using the
        pure-Python access-parser (no ODBC/mdbtools).

        Returns ``{filename, markdown, tables: [{name, columns, rows}], schema}``;
        ``422`` if the upload is not an Access file.
        """
        from docling_serve.access import (
            AccessToolsUnavailableError,
            access_to_markdown,
            is_access_file,
        )
        from docling_serve.access.extract import dump_schema

        tenant_id = deps.get_tenant_id(x_tenant_id)
        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = deps.safe_upload_name(upload.filename, "database.mdb")
        if not is_access_file(name):
            raise HTTPException(
                status_code=422, detail="extract/access expects a .mdb or .accdb file."
            )
        try:
            async with deps.admit_typed_upload(
                upload,
                tenant_id=tenant_id,
                domain="access",
                fallback_name="database.mdb",
            ) as admitted:
                markdown, tables = access_to_markdown(admitted.path)
                schema = dump_schema(admitted.path)
        except AccessToolsUnavailableError as err:
            raise HTTPException(status_code=503, detail=str(err)) from err
        return {
            "filename": name,
            "markdown": markdown,
            "tables": tables,
            "schema": schema,
        }

    async def execute_form(
        upload: UploadFile, name: str, tenant_id: str, prefix: str, bucket: str
    ) -> dict[str, Any]:
        from docling_serve.form import (
            XfaToolsUnavailableError,
            extract_xfa_form,
            is_xfa_pdf,
            read_xfa_packets,
        )

        if not name.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=422, detail="extract/form expects a .pdf file."
            )

        async def extract(
            src: Path, bundle: Path, prefix: str, target_bucket: str
        ) -> dict[str, Any]:
            try:
                if not is_xfa_pdf(src):
                    raise HTTPException(
                        status_code=422,
                        detail="This PDF carries no XFA form (not an AF/LiveCycle dynamic form).",
                    )
                payload = extract_xfa_form(src, source_key=name)
                packets = read_xfa_packets(src)
            except XfaToolsUnavailableError as err:
                raise HTTPException(status_code=503, detail=str(err)) from err

            if prefix and target_bucket:
                bundle.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
                (bundle / "form.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (bundle / "form.md").write_text(
                    payload.get("markdown") or "", encoding="utf-8"
                )
                (bundle / "xfa-fields.json").write_text(
                    json.dumps(
                        {
                            "source": name,
                            "fieldCount": payload.get("fieldCount"),
                            "labelCount": payload.get("labelCount"),
                            "boundValueCount": payload.get("boundValueCount"),
                            "fields": payload.get("fields") or [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                for packet_name in ("template", "datasets"):
                    if raw := packets.get(packet_name):
                        (bundle / f"xfa-{packet_name}.xml").write_bytes(raw)
                (bundle / "extraction.json").write_text(
                    json.dumps(
                        {
                            "domain": "form",
                            "form": {
                                "format": "xfa",
                                "payload": "form.json",
                                "markdown": "form.md",
                                "fieldCatalog": "xfa-fields.json",
                                "template": "xfa-template.xml",
                                "datasets": (
                                    "xfa-datasets.xml"
                                    if payload.get("hasDatasets")
                                    else None
                                ),
                            },
                            "fieldCount": payload.get("fieldCount"),
                            "sections": payload.get("sections"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            return published_result(payload, bundle, prefix, target_bucket)

        return await execute_typed_domain(
            upload,
            tenant_id=tenant_id,
            domain="form",
            fallback_name="form.pdf",
            work_prefix="xfa-form-",
            prefix=prefix,
            bucket=bucket,
            execute=extract,
        )

    # XFA / AF form — Adobe LiveCycle "dynamic" PDFs (AF IMT, DoD e-Publishing) hide
    # the real form in XML packets docling's PDF/OCR pipeline can't see, so this reads
    # the XFA packets directly (pikepdf) into the captify.form.v1 payload + a markdown
    # rendering the normal chunk/index/graph path can consume.
    @router.post(
        "/v1/extract/form",
        tags=["extract"],
        summary="Extract an XFA / Air Force dynamic PDF form (captify.form.v1)",
    )
    async def extract_form_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        """Extract an XFA PDF as ``captify.form.v1`` and optionally publish it."""
        tenant_id = deps.get_tenant_id(x_tenant_id)
        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = deps.safe_upload_name(upload.filename, "form.pdf")
        return await execute_form(upload, name, tenant_id, prefix, bucket)

    async def execute_technical_order(
        upload: UploadFile, name: str, tenant_id: str, prefix: str, bucket: str
    ) -> dict[str, Any]:
        from docling_serve.technical_order.extract import extract_technical_order

        if not name.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=422, detail="extract/technical-order expects a .pdf file."
            )

        async def extract(
            src: Path, bundle: Path, prefix: str, target_bucket: str
        ) -> dict[str, Any]:
            will_publish = bool(prefix and target_bucket)
            media_dir = bundle / "media" if will_publish else None
            vision_cfg = None
            if (
                deps.settings.figure_hotspot_vision
                and deps.settings.litellm_base_url
                and deps.settings.litellm_api_key
            ):
                vision_cfg = {
                    "base_url": deps.settings.litellm_base_url,
                    "api_key": deps.settings.litellm_api_key,
                    "model": deps.settings.bedrock_vision_model,
                    "min_recall": deps.settings.figure_hotspot_vision_min_recall,
                    "max_calls": deps.settings.figure_hotspot_vision_max_calls,
                    "fallback_model": deps.settings.technical_order_drawing_twin_model,
                    "parts_enabled": deps.settings.vision_parts,
                    "parts_max_pages": deps.settings.vision_parts_max_pages,
                }
            payload = await run_in_threadpool(
                extract_technical_order,
                src,
                source_key=name,
                media_dir=media_dir,
                vision=vision_cfg,
            )
            entry_count = int(payload.get("entryCount") or 0)
            figure_count = int(payload.get("figureCount") or 0)
            has_content = entry_count > 0 or figure_count > 0
            schematic_figures = None
            if will_publish and has_content:
                bundle.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
                if (
                    deps.settings.technical_order_schematic_figures
                    and deps.settings.bedrock_enabled
                    and figure_count > 0
                ):
                    from docling_serve.technical_order.mpl import FigureRecord
                    from docling_serve.technical_order.schematic_figures import (
                        extract_schematic_figure_bundle,
                    )

                    figure_records = [
                        FigureRecord(
                            figure_number=str(figure.get("figureNumber") or ""),
                            figure_title=str(figure.get("figureTitle") or ""),
                            sheet_number=str(figure.get("sheetNumber") or ""),
                            sheet_total=figure.get("sheetTotal"),
                            page_number=int(figure.get("pageNumber") or 0),
                            media_key=str(figure.get("mediaKey") or ""),
                            hotspots=list(figure.get("hotspots") or []),
                        )
                        for figure in payload.get("figures") or []
                        if isinstance(figure, dict)
                    ]
                    try:
                        schematic_figures = await run_in_threadpool(
                            extract_schematic_figure_bundle,
                            src,
                            figure_records,
                            bundle / "technical-order-schematics",
                            figure_only=entry_count == 0,
                            max_pages=deps.settings.technical_order_schematic_max_pages,
                        )
                    except Exception as err:
                        payload.setdefault("warnings", []).append(
                            f"schematic figure extraction failed: {err}"
                        )
                if schematic_figures:
                    payload["schematicFigures"] = schematic_figures
                    payload["bom"]["schematicFigures"] = schematic_figures
                if (
                    deps.settings.technical_order_drawing_twin
                    and deps.settings.litellm_base_url
                    and deps.settings.litellm_api_key
                    and entry_count > 0
                ):
                    from docling_serve.technical_order.drawing_twin import (
                        build_drawing_twin,
                    )

                    try:
                        twin = await run_in_threadpool(
                            build_drawing_twin,
                            payload.get("bom") or {},
                            bundle / "media",
                            base_url=deps.settings.litellm_base_url,
                            api_key=deps.settings.litellm_api_key,
                            model=deps.settings.technical_order_drawing_twin_model,
                            max_figures=deps.settings.technical_order_drawing_twin_max_figures,
                        )
                        (bundle / "drawing-twin.json").write_text(
                            json.dumps(twin, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        payload["bom"]["drawingTwin"] = "drawing-twin.json"
                    except Exception as err:
                        payload.setdefault("warnings", []).append(
                            f"drawing twin extraction failed: {err}"
                        )
                (bundle / "bom.json").write_text(
                    json.dumps(payload.get("bom") or {}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (bundle / "extraction.json").write_text(
                    json.dumps(
                        {
                            "domain": "technical-order",
                            "technicalOrder": {
                                "bom": "bom.json",
                                "schematicFigures": schematic_figures,
                                "drawingTwin": (payload.get("bom") or {}).get(
                                    "drawingTwin"
                                ),
                            },
                            "documentNumber": payload.get("documentNumber"),
                            "documentType": payload.get("documentType"),
                            "entryCount": payload.get("entryCount"),
                            "figureCount": payload.get("figureCount"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            return published_result(
                payload, bundle, prefix, target_bucket, enabled=has_content
            )

        return await execute_typed_domain(
            upload,
            tenant_id=tenant_id,
            domain="technical-order",
            fallback_name="to.pdf",
            work_prefix="technical-order-",
            prefix=prefix,
            bucket=bucket,
            execute=extract,
        )

    # Technical Order (IPB/RPSTL) — the master parts list is a layout-aligned table
    # docling's reading-order export doesn't preserve, so this runs the deterministic
    # poppler-layout + MPL parser as an added pass and returns the captify.bom.v1 payload.
    @router.post(
        "/v1/extract/technical-order",
        tags=["extract"],
        summary="Extract an IPB/RPSTL technical order's parts list (captify.bom.v1)",
    )
    async def extract_technical_order_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        """Extract an IPB/RPSTL PDF as ``captify.bom.v1`` and optionally publish it."""
        tenant_id = deps.get_tenant_id(x_tenant_id)
        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = deps.safe_upload_name(upload.filename, "to.pdf")
        return await execute_technical_order(upload, name, tenant_id, prefix, bucket)

    async def execute_schematic(
        upload: UploadFile,
        name: str,
        tenant_id: str,
        profile: str,
        prefix: str,
        bucket: str,
    ) -> dict[str, Any]:
        from docling_serve.schematic.extract import extract_schematic

        async def extract(
            src: Path, bundle: Path, prefix: str, target_bucket: str
        ) -> dict[str, Any]:
            result = extract_schematic(
                src,
                bundle,
                profile=profile,
                tenant_id=tenant_id,
                source_key=name,
            )
            return published_result(
                {
                    "domain": result["domain"],
                    "graph": result["graph"],
                    "artifacts": result["artifacts"],
                    "notes": result["notes"],
                },
                bundle,
                prefix,
                target_bucket,
            )

        return await execute_typed_domain(
            upload,
            tenant_id=tenant_id,
            domain="schematic",
            fallback_name="schematic.pdf",
            work_prefix="schematic-",
            prefix=prefix,
            bucket=bucket,
            execute=extract,
        )

    # Schematic (engineering drawing) extraction — the wiring GEOMETRY docling can't
    # recover (component symbols + the nets connecting their pins) is produced here as
    # a captify.schematic.v1 graph + derived artifacts (SVG, KiCad, netlist, EDML, XML).
    @router.post(
        "/v1/extract/schematic",
        tags=["extract"],
        summary="Extract an engineering drawing into a captify.schematic.v1 graph",
    )
    async def extract_schematic_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        profile: Annotated[str, Form()] = "schematic",
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        """Extract a ``captify.schematic.v1`` graph and optionally publish it."""
        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = deps.safe_upload_name(upload.filename, "schematic.pdf")
        tenant_id = deps.get_tenant_id(x_tenant_id)
        return await execute_schematic(upload, name, tenant_id, profile, prefix, bucket)

    @router.post(
        "/v1/extract/auto",
        tags=["extract"],
        summary="Select the authoritative extraction capability for a document",
    )
    async def extract_auto_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        profile: Annotated[str, Form()] = "auto",
        markdown: Annotated[str, Form()] = "",
        ocr_policy: Annotated[str, Form()] = "auto",
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        """Classify once and delegate to the registered domain implementation."""
        from docling_serve.capabilities import classify_document
        from docling_serve.ingestion.adapters import execute_adapter

        tenant_id = deps.get_tenant_id(x_tenant_id)
        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = deps.safe_upload_name(upload.filename, "document")
        data = await deps.read_upload_bytes(upload)
        try:
            decision = classify_document(
                filename=name,
                payload=data,
                profile=profile,
                markdown=markdown,
                ocr_policy=ocr_policy,
                min_parts_signals=deps.settings.auto_route_min_parts_signals,
                max_pdf_streams=deps.settings.auto_route_max_pdf_streams,
                max_stream_output_bytes=deps.settings.auto_route_max_stream_output_bytes,
                max_total_stream_output_bytes=deps.settings.auto_route_max_total_stream_output_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def _extract_access_adapter() -> dict:
            return await extract_access(auth, files, x_tenant_id)

        async def _extract_form_adapter() -> dict:
            return await execute_form(upload, name, tenant_id, prefix, bucket)

        async def _extract_technical_order_adapter() -> dict:
            return await execute_technical_order(
                upload, name, tenant_id, prefix, bucket
            )

        async def _extract_schematic_adapter() -> dict:
            return await execute_schematic(
                upload, name, tenant_id, "schematic", prefix, bucket
            )

        def _routing_only_adapter() -> dict:
            return {"domain": decision.domain}

        await upload.seek(0)
        result = await execute_adapter(
            decision.domain,
            {
                "document": _routing_only_adapter,
                "legacy-office": _routing_only_adapter,
                "access": _extract_access_adapter,
                "form": _extract_form_adapter,
                "technical-order": _extract_technical_order_adapter,
                "schematic": _extract_schematic_adapter,
            },
        )
        return {**result, "routing": decision.public_dict()}

    return router
