from __future__ import annotations

from types import SimpleNamespace

import pytest

from docling.datamodel.service.sources import FileSource

from docling_serve.ingestion.admission import UploadLimitExceeded, admit_upload
from docling_serve.upload_staging import StagedUpload, StagedUploadRef


class FakeUpload:
    filename = "folder/report.pdf"
    content_type = "application/pdf"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset : end]
        self.offset += len(chunk)
        return chunk


class FakeStager:
    def __init__(self) -> None:
        self.staged: list[dict[str, object]] = []
        self.cleaned: list[StagedUploadRef] = []

    async def stage(self, **kwargs: object) -> StagedUpload:
        self.staged.append(kwargs)
        ref = StagedUploadRef(
            upload_id="00000000-0000-0000-0000-000000000001",
            bucket_id="1" * 64,
            key="tenant/source",
            version_id="version-1",
            size_bytes=len(kwargs["payload"]),  # type: ignore[arg-type]
            checksum_sha256="0" * 64,
            original_name=str(kwargs["filename"]),
            content_type=str(kwargs["content_type"]),
            tenant_hash="1" * 64,
        )
        return StagedUpload(
            source=FileSource(filename="staged://upload-1", base64_string=""),
            ref=ref,
        )

    async def cleanup(self, refs: list[StagedUploadRef]) -> list[StagedUploadRef]:
        self.cleaned.extend(refs)
        return refs


def _settings(mode: str = "disabled") -> SimpleNamespace:
    return SimpleNamespace(
        upload_staging_mode=mode,
        max_file_size=8,
        legacy_office_max_input_bytes=4,
    )


@pytest.mark.asyncio
async def test_admission_uses_actual_bytes_and_cleans_materialization() -> None:
    async with admit_upload(
        FakeUpload(b"payload"),
        tenant_id="tenant-a",
        domain="document",
        settings=_settings(),
    ) as admitted:
        path = admitted.path
        assert admitted.filename == "report.pdf"
        assert admitted.path.read_bytes() == b"payload"
        assert admitted.tenant_id == "tenant-a"
    assert not path.exists()


@pytest.mark.asyncio
async def test_required_staging_preserves_tenant_and_cleans_remote_ref() -> None:
    stager = FakeStager()
    async with admit_upload(
        FakeUpload(b"payload"),
        tenant_id="tenant-a",
        domain="document",
        settings=_settings("required"),
        stager_factory=lambda: stager,
    ) as admitted:
        assert admitted.staged_refs
        assert stager.staged[0]["tenant_id"] == "tenant-a"
    assert stager.cleaned == admitted.staged_refs


@pytest.mark.asyncio
async def test_capability_specific_limit_fails_before_staging() -> None:
    stager = FakeStager()
    with pytest.raises(UploadLimitExceeded, match="4-byte"):
        async with admit_upload(
            FakeUpload(b"12345"),
            tenant_id="tenant-a",
            domain="legacy-office",
            settings=_settings("required"),
            stager_factory=lambda: stager,
        ):
            pass
    assert stager.staged == []
