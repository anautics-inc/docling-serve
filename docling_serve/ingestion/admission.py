"""Shared upload admission for typed and generic ingestion."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Protocol

from docling_serve.capabilities import DocumentDomain
from docling_serve.ingestion.adapters import get_adapter
from docling_serve.upload_staging import (
    StagedUploadRef,
    UploadStager,
    build_upload_stager,
)

_READ_CHUNK_BYTES = 1024 * 1024


class UploadSource(Protocol):
    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


class UploadAdmissionError(RuntimeError):
    status_code = 503


class UploadLimitExceeded(UploadAdmissionError):
    status_code = 413


@dataclass(slots=True)
class AdmittedSource:
    path: Path
    materialization_root: Path
    filename: str
    content_type: str
    tenant_id: str
    size: int
    staged_refs: list[StagedUploadRef] = field(default_factory=list)
    _stager: UploadStager | None = field(default=None, repr=False)

    async def cleanup(self) -> None:
        try:
            if self._stager is not None and self.staged_refs:
                await self._stager.cleanup(self.staged_refs)
        finally:
            await asyncio.to_thread(
                shutil.rmtree, self.materialization_root, ignore_errors=True
            )


def safe_upload_name(filename: str | None, fallback: str) -> str:
    name = PurePath((filename or "").replace("\\", "/")).name
    return name if name not in {"", ".", ".."} else fallback


async def read_actual_bytes(upload: UploadSource, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(min(_READ_CHUNK_BYTES, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UploadLimitExceeded(f"Upload exceeds the {limit}-byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


@contextlib.asynccontextmanager
async def admit_upload(
    upload: UploadSource,
    *,
    tenant_id: str,
    domain: DocumentDomain,
    settings: Any,
    fallback_name: str = "document",
    stager_factory: Callable[[], UploadStager] | None = None,
) -> AsyncIterator[AdmittedSource]:
    """Admit, optionally stage, materialize, and deterministically clean one upload."""
    adapter = get_adapter(domain)
    filename = safe_upload_name(upload.filename, fallback_name)
    content_type = upload.content_type or "application/octet-stream"
    payload = await read_actual_bytes(
        upload,
        limit=adapter.admission_limit(settings),
    )
    stager: UploadStager | None = None
    staged_refs: list[StagedUploadRef] = []
    if settings.upload_staging_mode == "required":
        stager_factory = stager_factory or build_upload_stager
        stager = stager_factory()
        staged = await stager.stage(
            payload=payload,
            filename=filename,
            content_type=content_type,
            tenant_id=tenant_id,
        )
        staged_refs.append(staged.ref)
    materialization_root = Path(tempfile.mkdtemp(prefix="docling-admitted-"))
    path = materialization_root / filename
    try:
        await asyncio.to_thread(path.write_bytes, payload)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, materialization_root, ignore_errors=True)
        if stager is not None and staged_refs:
            await stager.cleanup(staged_refs)
        raise
    admitted = AdmittedSource(
        path=path,
        materialization_root=materialization_root,
        filename=filename,
        content_type=content_type,
        tenant_id=tenant_id,
        size=len(payload),
        staged_refs=staged_refs,
        _stager=stager,
    )
    try:
        yield admitted
    finally:
        await admitted.cleanup()
