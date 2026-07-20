"""Shared authentication, tenant, orchestration, admission, and policy dependencies."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, UploadFile, WebSocket, status

from docling.datamodel.service.callbacks import CallbackSpec
from docling.datamodel.service.chunking import BaseChunkerOptions
from docling.datamodel.service.options import (
    ConvertDocumentsOptions as ConvertDocumentsRequestOptions,
)
from docling.datamodel.service.requests import (
    BatchConvertSourcesRequest,
    ConvertSourcesRequest,
    FileSourceRequest,
    GenericChunkDocumentsRequest,
    S3SourceRequest,
    TargetName,
    TargetRequest,
)
from docling.datamodel.service.sources import FileSource, HttpSource, S3Coordinates
from docling.datamodel.service.targets import (
    InBodyTarget,
    PresignedUrlTarget,
    ZipTarget,
)
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.chunking import ChunkingExportOptions
from docling_jobkit.datamodel.task import Task, TaskSource
from docling_jobkit.orchestrators.base_orchestrator import (
    BaseOrchestrator,
    TaskNotFoundError,
)

from docling_serve.auth import MachineAssertionAuth
from docling_serve.ingestion.admission import (
    UploadAdmissionError,
    admit_upload,
    read_actual_bytes,
)
from docling_serve.legacy_office import LEGACY_OFFICE_MIME_TYPES, is_legacy_office_name
from docling_serve.policy import (
    normalize_convert_options,
    normalize_request,
    validate_batch_convert_request,
    validate_chunk_request,
    validate_convert_options,
    validate_convert_request,
    validate_target_kind,
)
from docling_serve.public_errors import build_public_http_detail
from docling_serve.upload_staging import (
    STAGED_UPLOAD_METADATA_KEY,
    UploadStagingError,
    UploadStagingInputError,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    settings: Any
    require_auth: Any
    service_policy: Any
    convert_sources_request_model: Any
    default_target_name: TargetName
    models_ready: asyncio.Event
    queue_processor_failed: asyncio.Event
    orchestrator_provider: Any
    staging_capability_checker: Any
    upload_stager_builder: Any

    def authenticate_status_websocket(
        self,
        websocket: WebSocket,
        require_auth: Any,
        api_key: str,
        tenant_id: str | None,
    ) -> str | None:
        if self.settings.auth_mode == "assertion":
            assert isinstance(require_auth, MachineAssertionAuth)
            require_auth.authenticate(
                method="get",
                path=websocket.url.path,
                headers=websocket.headers,
            )
            signed_tenant = websocket.headers.get("x-captify-tenant-id")
            if tenant_id and tenant_id != signed_tenant:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Tenant query does not match the signed assertion.",
                )
            return signed_tenant
        if self.settings.auth_mode == "api_key" and self.settings.api_key:
            if api_key != self.settings.api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Api key is required as the ?api_key=SECRET query parameter.",
                )
        elif self.settings.auth_mode == "api_key" and not self.settings.allow_no_auth:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Server has no API key configured; refusing to serve requests "
                    "unauthenticated. Set DOCLING_SERVE_API_KEY, or set "
                    "DOCLING_SERVE_ALLOW_NO_AUTH=true for a deliberately "
                    "unauthenticated dev/test instance."
                ),
            )
        return tenant_id

    async def assert_ready(self) -> None:
        if self.settings.auth_mode == "assertion":
            assert isinstance(self.require_auth, MachineAssertionAuth)
            try:
                await asyncio.to_thread(self.require_auth.check_ready)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Machine assertion authentication is not ready: {exc}",
                ) from exc
        if not self.models_ready.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Models not yet loaded",
            )
        if self.queue_processor_failed.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Background queue processor is not running.",
            )
        if self.settings.upload_staging_mode == "required":
            try:
                await asyncio.to_thread(self.staging_capability_checker)
            except UploadStagingError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=exc.public_message,
                ) from exc
        orchestrator = self.orchestrator_provider()
        try:
            await orchestrator.check_connection()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=build_public_http_detail(
                    exc=exc,
                    debug_enabled=self.settings.debug_error_details,
                    fallback_message="Readiness check failed",
                ),
            ) from exc

    async def enqueue_source(
        self,
        orchestrator: BaseOrchestrator,
        request: (
            BatchConvertSourcesRequest
            | ConvertSourcesRequest
            | GenericChunkDocumentsRequest
        ),
        tenant_id: str | None = None,
    ) -> Task:
        sources: list[TaskSource] = []
        for s in request.sources:
            if isinstance(s, FileSourceRequest):
                sources.append(FileSource.model_validate(s))
            elif isinstance(s, HttpSource):
                sources.append(HttpSource.model_validate(s))
            elif isinstance(s, S3SourceRequest):
                sources.append(S3Coordinates.model_validate(s))

        convert_options: ConvertDocumentsRequestOptions
        chunking_options: BaseChunkerOptions | None = None
        chunking_export_options = ChunkingExportOptions()
        task_type: TaskType
        if isinstance(request, BatchConvertSourcesRequest | ConvertSourcesRequest):
            task_type = TaskType.CONVERT
            convert_options = request.options
        elif isinstance(request, GenericChunkDocumentsRequest):
            task_type = TaskType.CHUNK
            convert_options = request.convert_options
            chunking_options = request.chunking_options
            chunking_export_options.include_converted_doc = (
                request.include_converted_doc
            )
        else:
            raise RuntimeError("Uknown request type.")

        # Prepare metadata with tenant_id BEFORE enqueueing
        # This is critical because ray orchestrator reads tenant_id during enqueue()
        task_metadata: dict[str, str] = {}
        if tenant_id:
            task_metadata["tenant_id"] = tenant_id
            _log.info("[TENANT_ID] Preparing tenant-scoped task metadata")
        else:
            _log.warning("[TENANT_ID] No tenant_id provided, will use default")

        task = await orchestrator.enqueue(
            task_type=task_type,
            sources=sources,
            convert_options=convert_options,
            chunking_options=chunking_options,
            chunking_export_options=chunking_export_options,
            target=request.target,
            callbacks=request.callbacks,
            metadata=task_metadata,
        )

        _log.info("[TENANT_ID] Task %s created with tenant scope", task.task_id)

        return task

    async def enqueue_file(
        self,
        orchestrator: BaseOrchestrator,
        files: list[UploadFile],
        task_type: TaskType,
        convert_options: ConvertDocumentsRequestOptions,
        chunking_options: BaseChunkerOptions | None,
        chunking_export_options: ChunkingExportOptions | None,
        target: TargetRequest,
        callbacks: list[CallbackSpec] | None = None,
        tenant_id: str | None = None,
        task_metadata: dict[str, Any] | None = None,
    ) -> Task:
        _log.info("Enqueueing %s tenant-scoped file(s)", len(files))

        # Load the uploaded files to Docling DocumentStream
        file_sources: list[TaskSource] = []
        staged_refs = []
        try:
            stager = self.upload_stager_builder()
        except UploadStagingError as exc:
            raise HTTPException(status_code=503, detail=exc.public_message) from exc
        try:
            for i, file in enumerate(files):
                suffix = "" if len(files) == 1 else f"_{i}"
                name = file.filename if file.filename else f"file{suffix}.pdf"
                supplied_mime = file.content_type or "application/octet-stream"
                is_legacy = is_legacy_office_name(name) or supplied_mime.lower() in set(
                    LEGACY_OFFICE_MIME_TYPES.values()
                )
                from docling_serve.ingestion.adapters import get_adapter

                admission_limit = get_adapter(
                    "legacy-office" if is_legacy else "document"
                ).admission_limit(self.settings)
                file_bytes = await self.read_upload_bytes(
                    file, max_bytes=admission_limit
                )

                file_hash = hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()[
                    :12
                ]
                _log.info(
                    "File %s admitted: size=%s bytes, md5=%s",
                    i,
                    len(file_bytes),
                    file_hash,
                )
                staged = await stager.stage(
                    payload=file_bytes,
                    filename=name,
                    content_type=supplied_mime,
                    tenant_id=tenant_id or "default",
                )
                file_sources.append(staged.source)
                staged_refs.append(staged.ref)
        except UploadStagingInputError as exc:
            await stager.cleanup(staged_refs)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=exc.public_message,
            ) from exc
        except BaseException:
            await stager.cleanup(staged_refs)
            raise

        # Prepare metadata with tenant_id BEFORE enqueueing
        metadata: dict[str, Any] = {
            STAGED_UPLOAD_METADATA_KEY: [
                ref.model_dump(mode="json") for ref in staged_refs
            ],
            **(task_metadata or {}),
        }
        if tenant_id:
            metadata["tenant_id"] = tenant_id

        try:
            task = await orchestrator.enqueue(
                task_type=task_type,
                sources=file_sources,
                convert_options=convert_options,
                chunking_options=chunking_options,
                chunking_export_options=chunking_export_options,
                target=target,
                callbacks=callbacks or [],
                metadata=metadata,
            )
        except BaseException:
            await stager.cleanup(staged_refs)
            raise

        _log.info("[TENANT_ID] File task %s created with tenant scope", task.task_id)

        return task

    def get_tenant_id(self, tenant_id_header: str | None) -> str:
        """Return a validated tenant; authenticated work never shares a fallback."""
        tenant_id = (tenant_id_header or "").strip()
        if not tenant_id:
            if self.settings.auth_mode == "none" or self.settings.allow_default_tenant:
                return self.settings.default_tenant_id
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Missing required tenant header "
                    f"{self.settings.eng_ray_tenant_id_header}."
                ),
            )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", tenant_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Tenant identifier is invalid.",
            )
        _log.info("[TENANT_ID] Tenant scope extracted from request")
        return tenant_id

    def safe_upload_name(self, filename: str | None, fallback: str) -> str:
        """Bare file name for an upload — never a path.

        Client-supplied filenames name files inside per-request temp
        directories; a name carrying path separators or ``..`` would escape
        them (arbitrary file write as the service user). Reduce to the
        basename; fall back when nothing safe remains.
        """
        from pathlib import PurePosixPath, PureWindowsPath

        # Normalize both separator conventions before taking the basename.
        name = PurePosixPath(PureWindowsPath(str(filename or "")).as_posix()).name
        name = name.strip()
        if not name or name in {".", ".."}:
            return fallback
        return name

    def checked_upload_size(
        self, data: bytes, *, max_bytes: int | None = None
    ) -> bytes:
        """Enforce the configured ``max_file_size`` on a direct upload."""
        limit = max_bytes or self.settings.max_file_size
        if len(data) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Uploaded file exceeds the configured limit of {limit} bytes.",
            )
        return data

    async def read_upload_bytes(
        self,
        upload: UploadFile,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        """Stream through a hard actual-byte limit; never trust multipart size."""
        limit = max_bytes or self.settings.max_file_size
        try:
            return await read_actual_bytes(upload, limit=limit)
        except UploadAdmissionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @asynccontextmanager
    async def admit_typed_upload(
        self,
        upload: UploadFile,
        *,
        tenant_id: str,
        domain: Any,
        fallback_name: str,
    ):
        try:
            async with admit_upload(
                upload,
                tenant_id=tenant_id,
                domain=domain,
                settings=self.settings,
                fallback_name=fallback_name,
            ) as admitted:
                yield admitted
        except UploadAdmissionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except UploadStagingError as exc:
            raise HTTPException(status_code=503, detail=exc.public_message) from exc

    def validated_bundle_prefix(self, prefix: str) -> str:
        """A caller-supplied S3 bundle prefix, validated against traversal.

        Prefixes address bundle objects in S3 and, on check/revise, local
        paths derived from those keys — reject rooted, backslash-carrying,
        or dot-dot prefixes so a prefix can never step outside its keyspace.
        Returns the cleaned prefix ('' stays '').
        """
        cleaned = prefix.strip().strip("/")
        if not cleaned:
            return ""
        parts = cleaned.split("/")
        if "\\" in cleaned or any(part in {"", ".", ".."} for part in parts):
            raise HTTPException(status_code=422, detail="Invalid bundle prefix.")
        return cleaned

    def task_tenant_id(self, task: Task) -> str:
        """Return the tenant that owns a task, using only the explicit dev fallback."""
        return (task.metadata or {}).get("tenant_id") or self.settings.default_tenant_id

    def assert_task_tenant(self, task: Task, tenant_id: str) -> None:
        """Ensure the caller's tenant owns the task.

        Raises TaskNotFoundError (surfaced as 404) on mismatch rather than 403
        so a caller cannot probe whether a task UUID exists for another tenant.

        When tenants are not in use, every task is owned by 'default' and every
        caller resolves to 'default', so this check is transparent.
        """
        owner_tenant_id = self.task_tenant_id(task)
        if owner_tenant_id != tenant_id:
            _log.warning(
                f"[TENANT_ID] Tenant mismatch for task {task.task_id}: "
                f"caller='{tenant_id}' owner='{owner_tenant_id}' - denying access"
            )
            raise TaskNotFoundError()

    async def wait_task_complete(
        self, orchestrator: BaseOrchestrator, task_id: str
    ) -> bool:
        start_time = time.monotonic()
        while True:
            task = await orchestrator.task_status(task_id=task_id)
            if task.is_completed():
                return True
            await asyncio.sleep(self.settings.sync_poll_interval)
            elapsed_time = time.monotonic() - start_time
            if elapsed_time > self.settings.max_sync_wait:
                return False

    def prepare_convert_request(
        self,
        request: ConvertSourcesRequest,
    ) -> ConvertSourcesRequest:
        normalized_request = normalize_request(request, self.service_policy)
        validate_convert_request(normalized_request, self.service_policy)
        return normalized_request

    def prepare_batch_convert_request(
        self,
        request: BatchConvertSourcesRequest,
    ) -> BatchConvertSourcesRequest:
        normalized_request = normalize_request(request, self.service_policy)
        validate_batch_convert_request(normalized_request, self.service_policy)
        return normalized_request

    def prepare_chunk_request(
        self,
        request: GenericChunkDocumentsRequest,
    ) -> GenericChunkDocumentsRequest:
        normalized_request = request.model_copy(
            update={
                "convert_options": normalize_convert_options(
                    request.convert_options, self.service_policy
                )
            },
            deep=True,
        )
        validate_chunk_request(normalized_request, self.service_policy)
        return normalized_request

    def prepare_convert_options(
        self,
        options: ConvertDocumentsRequestOptions,
    ) -> ConvertDocumentsRequestOptions:
        normalized_options = normalize_convert_options(options, self.service_policy)
        validate_convert_options(normalized_options, self.service_policy)
        return normalized_options

    def validate_multipart_target_type(self, target_type: TargetName) -> None:
        validate_target_kind(target_type.value, self.service_policy)

    def check_file_upload(
        self, files: list[UploadFile], target_type: TargetName
    ) -> None:
        if len(files) > self.service_policy.max_sources_per_request:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Too many files: {len(files)} exceeds the "
                    f"maximum of {self.service_policy.max_sources_per_request}."
                ),
            )
        if (
            target_type == TargetName.PRESIGNED_URL
            and not self.service_policy.artifact_storage_enabled
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Presigned URL target requires artifact storage to be configured "
                    "and enabled on the server."
                ),
            )

    def resolve_file_target(self, target_type: TargetName) -> TargetRequest:
        if target_type == TargetName.PRESIGNED_URL:
            return PresignedUrlTarget()
        if target_type == TargetName.ZIP:
            return ZipTarget()
        return InBodyTarget()
