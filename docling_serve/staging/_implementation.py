"""IAM-only durable upload staging with private, typed task references."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
from typing import Any, BinaryIO, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from docling.datamodel.base_models import DocumentStream
from docling.datamodel.service.sources import FileSource
from docling_jobkit.datamodel.task import Task

from docling_serve.ingestion.source_identity import (
    PublicSourceIdentity as StagedPublicIdentity,
    bind_source_identities as bind_staged_identities,
    source_identities,
)

staged_identities = source_identities

STAGED_UPLOAD_METADATA_KEY = "_docling_private_staged_uploads"
STAGING_CLEANUP_KEY_PREFIX = "docling:staging-cleanup:"
STAGED_PLACEHOLDER_PREFIX = "__docling_staged_upload__"
STAGING_TAG_KEY = "docling-staging"
STAGING_TAG_VALUE = "true"
STAGING_LIFECYCLE_RULE_ID = "docling-staging-expiration"
STAGING_MULTIPART_LIFECYCLE_RULE_ID = "docling-staging-multipart-abort"
STAGING_CLEANUP_QUEUE_PREFIX = "docling-staging-cleanup/v1/queue/"
STAGING_CLEANUP_DEAD_PREFIX = "docling-staging-cleanup/v1/dead/"
STAGING_CLEANUP_CLAIM_PREFIX = "docling-staging-cleanup/v1/claims/"
STAGING_CLEANUP_LIFECYCLE_RULE_ID = "docling-staging-cleanup-expiration"
STAGING_DEAD_LIFECYCLE_RULE_ID = "docling-staging-dead-letter-expiration"
STAGING_CLAIM_LIFECYCLE_RULE_ID = "docling-staging-claim-expiration"
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}")
_MEDIA_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_CONTENT_TYPE_PATTERN = re.compile(rf"({_MEDIA_TOKEN})/({_MEDIA_TOKEN})")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(?:\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+|"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]+)"
)
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\b"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(r"(?i)\b(?:password|token|secret)\s*[:=]\s*\S+")
_AWS_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\bx-amz-(?:credential|signature|security-token)\s*[:=]\s*\S+"
)
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_AWS_QUERY_NAMES = {
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}
_TRANSIENT_DELETE_CODES = {
    "InternalError",
    "RequestTimeout",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
}
_CAPABILITY_CACHE: tuple[float, str] | None = None
_RECONCILER_OWNER_ID = uuid.uuid4().hex
_log = logging.getLogger(__name__)


class StreamingBodyProtocol(Protocol):
    def read(self, amount: int | None = None) -> bytes: ...

    def close(self) -> None: ...


class S3ClientProtocol(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_bucket_lifecycle_configuration(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...


class UploadStagingError(RuntimeError):
    public_message = "Durable upload staging is unavailable."


class UploadStagingDisabled(UploadStagingError):
    public_message = "File uploads are disabled on this deployment."


class UploadStagingCapabilityError(UploadStagingError):
    pass


class StagedUploadTamperedError(UploadStagingError):
    public_message = "Staged upload reference failed validation."


class StagedUploadLimitError(UploadStagingError):
    public_message = "Staged upload exceeds the configured size limit."


class StagedUploadCleanupError(UploadStagingError):
    public_message = "Staged upload cleanup could not be completed."


class UploadStagingInputError(UploadStagingError):
    public_message = "Upload filename or media type is invalid."


def _reject_controls(value: str, field: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value


def contains_bearer_syntax(value: str) -> bool:
    return bool(
        _AUTHORIZATION_PATTERN.search(value)
        or _JWT_PATTERN.search(value)
        or _SECRET_ASSIGNMENT_PATTERN.search(value)
        or _AWS_ASSIGNMENT_PATTERN.search(value)
        or _AWS_ACCESS_KEY_PATTERN.search(value)
    )


def _reject_bearer_syntax(value: str, field: str) -> str:
    _reject_controls(value, field)
    if contains_bearer_syntax(value):
        raise ValueError(f"{field} contains bearer credential syntax")
    return value


def _safe_error_code(value: Any) -> str:
    candidate = str(value or "UnclassifiedError")
    if (
        len(candidate) <= 64
        and not contains_bearer_syntax(candidate)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", candidate) is not None
    ):
        return candidate
    return "UnclassifiedError"


def _is_precondition_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return bool(
        status == 412
        or (
            isinstance(error, dict)
            and error.get("Code") in {"PreconditionFailed", "412"}
        )
    )


def _response_etag(response: dict[str, Any]) -> str:
    etag = response.get("ETag")
    if (
        not isinstance(etag, str)
        or not etag
        or len(etag) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in etag)
    ):
        raise StagedUploadTamperedError("S3 response did not include a valid ETag.")
    return etag


class StagedUploadRef(BaseModel):
    """Private serializable reference; contains no bearer credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_id: str
    bucket_id: str
    key: str
    version_id: str | None = None
    checksum_sha256: str
    size_bytes: int = Field(ge=0)
    content_type: str
    original_name: str
    original_uri: str | None = None
    tenant_hash: str
    cleanup_status: Literal["pending", "deleted", "retry", "dead"] = "pending"
    cleanup_attempts: int = Field(default=0, ge=0)
    cleanup_next_at: float | None = Field(default=None, ge=0)
    cleanup_error_code: str | None = None

    @field_validator("upload_id")
    @classmethod
    def validate_upload_id(cls, value: str) -> str:
        try:
            return uuid.UUID(value).hex
        except ValueError as exc:
            raise ValueError("upload_id must be an opaque UUID") from exc

    @field_validator("bucket_id", "checksum_sha256", "tenant_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if _CHECKSUM_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid SHA-256 value")
        return value

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: str) -> str:
        _reject_bearer_syntax(value, "content_type")
        if (
            len(value) > 127
            or "://" in value
            or "?" in value
            or "#" in value
            or _CONTENT_TYPE_PATTERN.fullmatch(value) is None
        ):
            raise ValueError("content_type must be a base type/subtype media type")
        return value.lower()

    @field_validator("original_name")
    @classmethod
    def validate_original_name(cls, value: str) -> str:
        _reject_bearer_syntax(value, "original_name")
        safe = PurePath(value.replace("\\", "/")).name
        if safe in {"", ".", ".."} or safe != value or len(safe) > 255:
            raise ValueError("invalid original filename")
        return safe

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        _reject_bearer_syntax(value, "key")
        if not value or len(value) > 1024:
            raise ValueError("invalid staged object key")
        return value

    @field_validator("version_id")
    @classmethod
    def validate_version_id(cls, value: str | None) -> str | None:
        if value is not None:
            _reject_bearer_syntax(value, "version_id")
        if value is not None and (not value or len(value) > 1024):
            raise ValueError("invalid object version")
        return value

    @field_validator("original_uri")
    @classmethod
    def validate_original_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _reject_bearer_syntax(value, "original_uri")
        if len(value) > 2048 or value != value.strip():
            raise ValueError("invalid original URI")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("original URI must not contain credentials or query data")
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        query_names = {name.lower() for name, _value in query_items}
        if query_names & _AWS_QUERY_NAMES:
            raise ValueError("original URI contains an AWS bearer query parameter")
        if any(
            re.fullmatch(r"(?i)(?:password|token|secret)", name) for name in query_names
        ):
            raise ValueError("original URI contains a credential assignment")
        for name, query_value in query_items:
            _reject_bearer_syntax(name, "original_uri")
            _reject_bearer_syntax(query_value, "original_uri")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @field_validator("cleanup_error_code")
    @classmethod
    def validate_cleanup_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _reject_bearer_syntax(value, "cleanup_error_code")
        if len(value) > 64 or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value) is None:
            raise ValueError("invalid cleanup error code")
        return value


class CleanupQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_id: str
    ref: StagedUploadRef
    attempt: int = Field(default=0, ge=0)
    next_at: float = Field(ge=0)
    error_code: str | None = None

    @field_validator("queue_id")
    @classmethod
    def validate_queue_id(cls, value: str) -> str:
        try:
            return uuid.UUID(value).hex
        except ValueError as exc:
            raise ValueError("queue_id must be an opaque UUID") from exc

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _reject_bearer_syntax(value, "error_code")
        if len(value) > 64 or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value) is None:
            raise ValueError("invalid cleanup error code")
        return value


class CleanupClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_id: str
    owner_id: str
    token: str
    expires_at: float = Field(ge=0)

    @field_validator("queue_id", "owner_id", "token")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        try:
            return uuid.UUID(value).hex
        except ValueError as exc:
            raise ValueError("cleanup claim identifiers must be opaque UUIDs") from exc


@dataclass(frozen=True)
class CleanupQueueRecord:
    item: CleanupQueueItem
    key: str
    etag: str


@dataclass(frozen=True)
class CleanupClaim:
    payload: CleanupClaimPayload
    key: str
    etag: str


@dataclass(frozen=True)
class StagedUpload:
    source: FileSource
    ref: StagedUploadRef


class UploadStager(Protocol):
    async def stage(
        self,
        *,
        payload: bytes,
        filename: str,
        content_type: str,
        tenant_id: str,
    ) -> StagedUpload: ...

    async def cleanup(self, refs: list[StagedUploadRef]) -> list[StagedUploadRef]: ...


def _safe_filename(filename: str) -> str:
    name = PurePath(filename.replace("\\", "/")).name
    return name if name not in {"", ".", ".."} else "upload.bin"


def _tenant_hash(tenant_id: str, upload_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}\0{upload_id}".encode()).hexdigest()


def _bucket_id(bucket: str, endpoint: str, region: str) -> str:
    return hashlib.sha256(f"{bucket}\0{endpoint}\0{region}".encode()).hexdigest()


def _placeholder(upload_id: str) -> FileSource:
    return FileSource(
        filename=f"{STAGED_PLACEHOLDER_PREFIX}{upload_id}",
        base64_string="",
    )


def is_staged_placeholder(source: Any) -> bool:
    return isinstance(source, FileSource) and source.filename.startswith(
        STAGED_PLACEHOLDER_PREFIX
    )


def _placeholder_id(source: FileSource) -> str:
    value = source.filename.removeprefix(STAGED_PLACEHOLDER_PREFIX)
    try:
        return uuid.UUID(value).hex
    except ValueError as exc:
        raise StagedUploadTamperedError("Invalid staged upload placeholder.") from exc


def _parse_refs(task: Task) -> list[StagedUploadRef]:
    raw = task.metadata.get(STAGED_UPLOAD_METADATA_KEY, [])
    if not isinstance(raw, list):
        raise StagedUploadTamperedError("Staged upload metadata must be a list.")
    try:
        return [StagedUploadRef.model_validate(item) for item in raw]
    except ValueError as exc:
        raise StagedUploadTamperedError("Invalid staged upload metadata.") from exc


def staged_refs_for_task(task: Task) -> list[StagedUploadRef]:
    return _parse_refs(task)


class S3UploadStager:
    """S3 staging restricted to one configured bucket and fixed prefix."""

    def __init__(
        self,
        *,
        client: S3ClientProtocol,
        bucket: str,
        bucket_id: str,
        prefix: str,
        endpoint: str,
        region: str,
        retention_days: int,
        cleanup_retention_days: int,
        dead_letter_retention_days: int,
        claim_retention_days: int,
        claim_lease_seconds: float,
        max_file_size: int,
        kms_key_id: str | None,
        cleanup_retries: int,
    ):
        self.client = client
        self.bucket = bucket
        self.bucket_id = bucket_id
        self.prefix = prefix.strip("/") + "/"
        self.endpoint = endpoint
        self.region = region
        self.retention_days = retention_days
        self.cleanup_retention_days = cleanup_retention_days
        self.dead_letter_retention_days = dead_letter_retention_days
        self.claim_retention_days = claim_retention_days
        self.claim_lease_seconds = claim_lease_seconds
        self.max_file_size = max_file_size
        self.kms_key_id = kms_key_id
        self.cleanup_retries = cleanup_retries

    def _expected_key(self, upload_id: str, tenant_hash: str) -> str:
        return f"{self.prefix}{tenant_hash}/{upload_id}"

    def validate_ref(self, ref: StagedUploadRef, tenant_id: str) -> None:
        expected_tenant = _tenant_hash(tenant_id, ref.upload_id)
        expected_key = self._expected_key(ref.upload_id, expected_tenant)
        if (
            ref.bucket_id != self.bucket_id
            or ref.tenant_hash != expected_tenant
            or ref.key != expected_key
            or not ref.key.startswith(self.prefix)
            or ref.size_bytes > self.max_file_size
        ):
            raise StagedUploadTamperedError(
                "Staged upload bucket, prefix, tenant, or size did not match policy."
            )

    def _encryption_args(self) -> dict[str, str]:
        if self.kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    async def stage(
        self,
        *,
        payload: bytes,
        filename: str,
        content_type: str,
        tenant_id: str,
    ) -> StagedUpload:
        if len(payload) > self.max_file_size:
            raise StagedUploadLimitError("Upload exceeds staging size limit.")
        safe_name = _safe_filename(filename)
        upload_id = uuid.uuid4().hex
        tenant_hash = _tenant_hash(tenant_id, upload_id)
        key = self._expected_key(upload_id, tenant_hash)
        checksum = hashlib.sha256(payload).hexdigest()
        try:
            base_ref = StagedUploadRef(
                upload_id=upload_id,
                bucket_id=self.bucket_id,
                key=key,
                checksum_sha256=checksum,
                size_bytes=len(payload),
                content_type=content_type,
                original_name=safe_name,
                original_uri=None,
                tenant_hash=tenant_hash,
            )
        except ValueError as exc:
            raise UploadStagingInputError(
                "Upload filename or media type failed validation."
            ) from exc

        def put() -> StagedUpload:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=payload,
                ContentLength=len(payload),
                ContentType=base_ref.content_type,
                Metadata={
                    "upload-id": upload_id,
                    "checksum-sha256": checksum,
                    "tenant-hash": tenant_hash,
                    "original-name-sha256": hashlib.sha256(
                        safe_name.encode()
                    ).hexdigest(),
                },
                Tagging=f"{STAGING_TAG_KEY}={STAGING_TAG_VALUE}",
                **self._encryption_args(),
            )
            ref = StagedUploadRef.model_validate(
                {
                    **base_ref.model_dump(mode="python"),
                    "version_id": response.get("VersionId"),
                }
            )
            return StagedUpload(source=_placeholder(upload_id), ref=ref)

        return await asyncio.to_thread(put)

    def materialize(self, ref: StagedUploadRef, tenant_id: str) -> BinaryIO:
        self.validate_ref(ref, tenant_id)
        head_args: dict[str, Any] = {"Bucket": self.bucket, "Key": ref.key}
        if ref.version_id:
            head_args["VersionId"] = ref.version_id
        head = self.client.head_object(**head_args)
        metadata = head.get("Metadata") or {}
        encryption = head.get("ServerSideEncryption")
        if self.kms_key_id:
            encryption_valid = (
                encryption == "aws:kms" and head.get("SSEKMSKeyId") == self.kms_key_id
            )
        else:
            encryption_valid = encryption == "AES256"
        if (
            int(head.get("ContentLength", -1)) != ref.size_bytes
            or head.get("ContentType") != ref.content_type
            or metadata.get("upload-id") != ref.upload_id
            or metadata.get("checksum-sha256") != ref.checksum_sha256
            or metadata.get("tenant-hash") != ref.tenant_hash
            or metadata.get("original-name-sha256")
            != hashlib.sha256(ref.original_name.encode()).hexdigest()
            or not encryption_valid
        ):
            raise StagedUploadTamperedError(
                "Staged object metadata or encryption did not match its reference."
            )

        resources = ExitStack()
        try:
            response = self.client.get_object(**head_args)
            body = cast(StreamingBodyProtocol, response["Body"])
            resources.callback(body.close)
            # Docling's InputDocument accepts io.BytesIO or pathlib.Path, not
            # generic file-like objects such as SpooledTemporaryFile. The
            # staged object was already admitted under max_file_size, and the
            # guarded read below enforces that bound again.
            stream = BytesIO()
            resources.callback(stream.close)
            digest = hashlib.sha256()
            bytes_seen = 0
            while True:
                chunk = body.read(64 * 1024)
                if not chunk:
                    break
                bytes_seen += len(chunk)
                if bytes_seen > ref.size_bytes or bytes_seen > self.max_file_size:
                    raise StagedUploadLimitError(
                        "Staged object exceeded its declared size."
                    )
                digest.update(chunk)
                stream.write(chunk)
            body.close()
            if (
                bytes_seen != ref.size_bytes
                or digest.hexdigest() != ref.checksum_sha256
            ):
                raise StagedUploadTamperedError(
                    "Staged object bytes did not match the signed checksum."
                )
            stream.seek(0)
            resources.pop_all()
            return cast(BinaryIO, stream)
        except BaseException:
            resources.close()
            raise

    def cleanup_sync(self, refs: list[StagedUploadRef]) -> list[StagedUploadRef]:
        pending = [ref for ref in refs if ref.cleanup_status != "deleted"]
        if not pending:
            return refs
        for ref in pending:
            if (
                ref.bucket_id != self.bucket_id
                or ref.key != self._expected_key(ref.upload_id, ref.tenant_hash)
                or not ref.key.startswith(self.prefix)
            ):
                raise StagedUploadTamperedError("Cleanup reference escaped policy.")

        states = {ref.upload_id: ref for ref in refs}
        remaining = pending
        last_errors: dict[str, str] = {}
        for attempt in range(self.cleanup_retries + 1):
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={
                    "Objects": [
                        {
                            "Key": ref.key,
                            **(
                                {"VersionId": ref.version_id}
                                if ref.version_id is not None
                                else {}
                            ),
                        }
                        for ref in remaining
                    ],
                    "Quiet": False,
                },
            )
            errors = {
                (item.get("Key"), item.get("VersionId")): item
                for item in response.get("Errors", [])
            }
            next_remaining: list[StagedUploadRef] = []
            for ref in remaining:
                error = errors.get((ref.key, ref.version_id)) or errors.get(
                    (ref.key, None)
                )
                if error is None:
                    states[ref.upload_id] = ref.model_copy(
                        update={
                            "cleanup_status": "deleted",
                            "cleanup_attempts": ref.cleanup_attempts + attempt + 1,
                            "cleanup_next_at": None,
                            "cleanup_error_code": None,
                        }
                    )
                elif error.get("Code") in _TRANSIENT_DELETE_CODES:
                    last_errors[ref.upload_id] = _safe_error_code(error.get("Code"))
                    next_remaining.append(ref)
                else:
                    error_code = _safe_error_code(error.get("Code"))
                    states[ref.upload_id] = ref.model_copy(
                        update={
                            "cleanup_status": "dead",
                            "cleanup_attempts": ref.cleanup_attempts + attempt + 1,
                            "cleanup_next_at": None,
                            "cleanup_error_code": error_code,
                        }
                    )
            remaining = next_remaining
            if not remaining:
                break
            if attempt < self.cleanup_retries:
                time.sleep(min(0.1 * (2**attempt), 1.0))
        for ref in remaining:
            states[ref.upload_id] = ref.model_copy(
                update={
                    "cleanup_status": "retry",
                    "cleanup_attempts": ref.cleanup_attempts + self.cleanup_retries + 1,
                    "cleanup_next_at": time.time()
                    + min(
                        3600.0,
                        30.0
                        * (2 ** min(ref.cleanup_attempts + self.cleanup_retries, 7)),
                    ),
                    "cleanup_error_code": last_errors.get(
                        ref.upload_id, "TransientDeleteError"
                    ),
                }
            )
        return [states[ref.upload_id] for ref in refs]

    async def cleanup(self, refs: list[StagedUploadRef]) -> list[StagedUploadRef]:
        return await asyncio.to_thread(self.cleanup_sync, refs)

    def check_capability(self) -> None:
        self._validate_lifecycle()
        canary_id = uuid.uuid4().hex
        canary_key = f"{self.prefix}canary/{canary_id}"
        payload = b"docling-staging-canary"
        checksum = hashlib.sha256(payload).hexdigest()
        try:
            put = self.client.put_object(
                Bucket=self.bucket,
                Key=canary_key,
                Body=payload,
                ContentLength=len(payload),
                ContentType="application/octet-stream",
                Metadata={
                    "upload-id": canary_id,
                    "checksum-sha256": checksum,
                    "tenant-hash": "canary",
                },
                Tagging=f"{STAGING_TAG_KEY}={STAGING_TAG_VALUE}",
                **self._encryption_args(),
            )
            version_id = put.get("VersionId")
            args = {"Bucket": self.bucket, "Key": canary_key}
            if version_id:
                args["VersionId"] = version_id
            head = self.client.head_object(**args)
            if int(head.get("ContentLength", -1)) != len(payload):
                raise UploadStagingCapabilityError(
                    "Staging canary HEAD returned an unexpected size."
                )
            if self.kms_key_id:
                encryption_valid = (
                    head.get("ServerSideEncryption") == "aws:kms"
                    and head.get("SSEKMSKeyId") == self.kms_key_id
                )
            else:
                encryption_valid = head.get("ServerSideEncryption") == "AES256"
            if not encryption_valid:
                raise UploadStagingCapabilityError(
                    "Staging canary encryption did not match policy."
                )
            body = self.client.get_object(**args)["Body"]
            try:
                received = body.read(len(payload) + 1)
            finally:
                body.close()
            if received != payload:
                raise UploadStagingCapabilityError(
                    "Staging canary GET returned unexpected bytes."
                )
        except UploadStagingError:
            raise
        except Exception as exc:
            raise UploadStagingCapabilityError(
                "Staging put/head/get canary failed."
            ) from exc
        finally:
            try:
                response = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        "Objects": [
                            {
                                "Key": canary_key,
                                **(
                                    {"VersionId": version_id}
                                    if "version_id" in locals() and version_id
                                    else {}
                                ),
                            }
                        ],
                        "Quiet": False,
                    },
                )
                if response.get("Errors"):
                    raise UploadStagingCapabilityError(
                        "Staging canary DELETE returned object errors."
                    )
            except UploadStagingCapabilityError:
                raise
            except Exception as exc:
                raise UploadStagingCapabilityError(
                    "Staging canary DELETE failed."
                ) from exc
        self._check_cleanup_store_capability()

    def _check_cleanup_store_capability(self) -> None:
        upload_id = uuid.uuid4().hex
        tenant_hash = hashlib.sha256(b"cleanup-canary").hexdigest()
        ref = StagedUploadRef(
            upload_id=upload_id,
            bucket_id=self.bucket_id,
            key=self._expected_key(upload_id, tenant_hash),
            checksum_sha256=hashlib.sha256(b"").hexdigest(),
            size_bytes=0,
            content_type="application/octet-stream",
            original_name="canary.bin",
            tenant_hash=tenant_hash,
            cleanup_status="retry",
            cleanup_next_at=time.time(),
            cleanup_error_code="Canary",
        )
        store = S3CleanupStore(self)
        item = store.enqueue_ref(ref)
        try:
            due = store.due(
                now=time.time() + 1,
                limit=1,
                prefix=store._key(item),
            )
            if len(due) != 1 or due[0].item != item:
                raise UploadStagingCapabilityError(
                    "Encrypted cleanup queue canary was not readable."
                )
            claim = store.claim(
                due[0],
                owner_id=uuid.uuid4().hex,
                now=time.time(),
            )
            if claim is None:
                raise UploadStagingCapabilityError(
                    "Cleanup queue conditional claim canary failed."
                )
            store.complete(due[0], claim)
            store.release_claim(claim)
        finally:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=store._key(item),
            )
            self.client.delete_object(
                Bucket=self.bucket,
                Key=store._claim_key(item.queue_id),
            )

    def _validate_lifecycle(self) -> None:
        try:
            config = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket)
        except Exception as exc:
            raise UploadStagingCapabilityError(
                "Staging bucket lifecycle could not be read."
            ) from exc
        rules = config.get("Rules", [])
        expected = (
            (
                STAGING_LIFECYCLE_RULE_ID,
                self.prefix,
                self.retention_days,
                True,
                False,
            ),
            (
                STAGING_CLEANUP_LIFECYCLE_RULE_ID,
                STAGING_CLEANUP_QUEUE_PREFIX,
                self.cleanup_retention_days,
                False,
                True,
            ),
            (
                STAGING_DEAD_LIFECYCLE_RULE_ID,
                STAGING_CLEANUP_DEAD_PREFIX,
                self.dead_letter_retention_days,
                False,
                True,
            ),
            (
                STAGING_CLAIM_LIFECYCLE_RULE_ID,
                STAGING_CLEANUP_CLAIM_PREFIX,
                self.claim_retention_days,
                False,
                True,
            ),
        )
        for rule_id, prefix, days, tagged, require_abort in expected:
            matches = [
                rule
                for rule in rules
                if isinstance(rule, dict) and rule.get("ID") == rule_id
            ]
            if len(matches) != 1 or not _lifecycle_rule_matches(
                matches[0],
                prefix=prefix,
                days=days,
                tagged=tagged,
                require_abort=require_abort,
            ):
                raise UploadStagingCapabilityError(
                    "Required staging lifecycle rules are missing, disabled, "
                    "or too permissive."
                )
        multipart_matches = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("ID") == STAGING_MULTIPART_LIFECYCLE_RULE_ID
        ]
        if len(multipart_matches) != 1 or not _multipart_lifecycle_rule_matches(
            multipart_matches[0],
            prefix=self.prefix,
            days=self.retention_days,
        ):
            raise UploadStagingCapabilityError(
                "Required staging multipart lifecycle rule is missing, disabled, "
                "or too permissive."
            )


class S3CleanupStore:
    """Private SSE-encrypted durable cleanup work queue in the staging bucket."""

    def __init__(self, stager: S3UploadStager):
        self.stager = stager

    def _key(self, item: CleanupQueueItem, *, dead: bool = False) -> str:
        prefix = STAGING_CLEANUP_DEAD_PREFIX if dead else STAGING_CLEANUP_QUEUE_PREFIX
        return f"{prefix}{item.queue_id}"

    def _claim_key(self, queue_id: str) -> str:
        return f"{STAGING_CLEANUP_CLAIM_PREFIX}{queue_id}"

    def enqueue_ref(self, ref: StagedUploadRef) -> CleanupQueueItem:
        item = CleanupQueueItem(
            queue_id=uuid.uuid4().hex,
            ref=ref,
            attempt=ref.cleanup_attempts,
            next_at=ref.cleanup_next_at or time.time(),
            error_code=ref.cleanup_error_code,
        )
        is_dead = ref.cleanup_status == "dead"
        self.save(item, dead=is_dead, create=True)
        if is_dead:
            _log.error(
                "Staged cleanup permanently failed; encrypted audit item retained "
                "(error_code=%s)",
                item.error_code or "UnclassifiedError",
            )
        return item

    def save(
        self,
        item: CleanupQueueItem,
        *,
        dead: bool = False,
        create: bool = False,
        expected_etag: str | None = None,
    ) -> str:
        payload = item.model_dump_json().encode()
        conditions: dict[str, str] = {}
        if create:
            conditions["IfNoneMatch"] = "*"
        elif expected_etag is not None:
            conditions["IfMatch"] = expected_etag
        response = self.stager.client.put_object(
            Bucket=self.stager.bucket,
            Key=self._key(item, dead=dead),
            Body=payload,
            ContentLength=len(payload),
            ContentType="application/json",
            Metadata={"queue-item": "cleanup-v1"},
            **self.stager._encryption_args(),
            **conditions,
        )
        return _response_etag(response)

    def due(
        self,
        *,
        now: float,
        limit: int,
        prefix: str = STAGING_CLEANUP_QUEUE_PREFIX,
    ) -> list[CleanupQueueRecord]:
        if limit <= 0:
            return []
        records: list[CleanupQueueRecord] = []
        continuation_token: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.stager.bucket,
                "Prefix": prefix,
                "MaxKeys": min(1000, max(limit * 4, limit)),
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            listing = self.stager.client.list_objects_v2(**request)
            for entry in listing.get("Contents", []):
                key = entry.get("Key")
                etag = entry.get("ETag")
                if not isinstance(key, str) or not key.startswith(
                    STAGING_CLEANUP_QUEUE_PREFIX
                ):
                    continue
                if not isinstance(etag, str) or not etag:
                    raise StagedUploadTamperedError(
                        "Cleanup queue listing omitted an ETag."
                    )
                resources = ExitStack()
                try:
                    response = self.stager.client.get_object(
                        Bucket=self.stager.bucket,
                        Key=key,
                        IfMatch=etag,
                    )
                    body = cast(StreamingBodyProtocol, response["Body"])
                    resources.callback(body.close)
                    raw = body.read(64 * 1024 + 1)
                    if len(raw) > 64 * 1024:
                        raise StagedUploadTamperedError(
                            "Cleanup queue item exceeded its size limit."
                        )
                    item = CleanupQueueItem.model_validate_json(raw)
                    if key != self._key(item):
                        raise StagedUploadTamperedError(
                            "Cleanup queue object key did not match its item."
                        )
                    if item.next_at <= now:
                        records.append(
                            CleanupQueueRecord(item=item, key=key, etag=etag)
                        )
                        if len(records) >= limit:
                            return records
                finally:
                    resources.close()
            if not listing.get("IsTruncated"):
                return records
            next_token = listing.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token:
                raise StagedUploadTamperedError(
                    "Cleanup queue listing omitted its continuation token."
                )
            if next_token == continuation_token:
                raise StagedUploadTamperedError(
                    "Cleanup queue listing repeated its continuation token."
                )
            continuation_token = next_token

    def claim(
        self,
        record: CleanupQueueRecord,
        *,
        owner_id: str,
        now: float,
    ) -> CleanupClaim | None:
        payload = CleanupClaimPayload(
            queue_id=record.item.queue_id,
            owner_id=owner_id,
            token=uuid.uuid4().hex,
            expires_at=now + self.stager.claim_lease_seconds,
        )
        key = self._claim_key(record.item.queue_id)
        encoded = payload.model_dump_json().encode()
        args = {
            "Bucket": self.stager.bucket,
            "Key": key,
            "Body": encoded,
            "ContentLength": len(encoded),
            "ContentType": "application/json",
            "Metadata": {"claim": "cleanup-v1"},
            **self.stager._encryption_args(),
        }
        try:
            response = self.stager.client.put_object(**args, IfNoneMatch="*")
            return CleanupClaim(
                payload=payload,
                key=key,
                etag=_response_etag(response),
            )
        except Exception as exc:
            if not _is_precondition_failed(exc):
                raise

        existing_response = self.stager.client.get_object(
            Bucket=self.stager.bucket,
            Key=key,
        )
        existing_etag = _response_etag(existing_response)
        resources = ExitStack()
        try:
            body = cast(StreamingBodyProtocol, existing_response["Body"])
            resources.callback(body.close)
            raw = body.read(4097)
            if len(raw) > 4096:
                raise StagedUploadTamperedError(
                    "Cleanup claim exceeded its size limit."
                )
            existing = CleanupClaimPayload.model_validate_json(raw)
        finally:
            resources.close()
        if existing.queue_id != record.item.queue_id or existing.expires_at > now:
            return None
        try:
            response = self.stager.client.put_object(
                **args,
                IfMatch=existing_etag,
            )
        except Exception as exc:
            if _is_precondition_failed(exc):
                return None
            raise
        return CleanupClaim(
            payload=payload,
            key=key,
            etag=_response_etag(response),
        )

    def assert_claim(self, claim: CleanupClaim) -> None:
        if claim.payload.expires_at <= time.time():
            raise StagedUploadCleanupError("Cleanup claim lease expired.")
        response = self.stager.client.head_object(
            Bucket=self.stager.bucket,
            Key=claim.key,
            IfMatch=claim.etag,
        )
        if _response_etag(response) != claim.etag:
            raise StagedUploadCleanupError("Cleanup claim fencing check failed.")

    def release_claim(self, claim: CleanupClaim) -> None:
        try:
            self.stager.client.delete_object(
                Bucket=self.stager.bucket,
                Key=claim.key,
                IfMatch=claim.etag,
            )
        except Exception as exc:
            if not _is_precondition_failed(exc):
                raise

    def complete(self, record: CleanupQueueRecord, claim: CleanupClaim) -> None:
        self.assert_claim(claim)
        self.stager.client.delete_object(
            Bucket=self.stager.bucket,
            Key=record.key,
            IfMatch=record.etag,
        )

    def dead_letter(
        self,
        record: CleanupQueueRecord,
        item: CleanupQueueItem,
        claim: CleanupClaim,
    ) -> None:
        self.assert_claim(claim)
        try:
            self.save(item, dead=True, create=True)
        except Exception as exc:
            if not _is_precondition_failed(exc):
                raise
        self.complete(record, claim)
        _log.error(
            "Staged cleanup permanently failed; encrypted audit item retained "
            "(error_code=%s)",
            item.error_code or "UnclassifiedError",
        )


def reconcile_cleanup_once(
    stager: S3UploadStager,
    *,
    now: float | None = None,
    max_items: int = 32,
    owner_id: str = _RECONCILER_OWNER_ID,
) -> int:
    """Process a bounded durable cleanup batch; safe across worker restarts."""

    store = S3CleanupStore(stager)
    processed = 0
    effective_now = now if now is not None else time.time()
    for record in store.due(now=effective_now, limit=max_items):
        claim = store.claim(record, owner_id=owner_id, now=effective_now)
        if claim is None:
            continue
        try:
            store.assert_claim(claim)
            states = stager.cleanup_sync([record.item.ref])
            state = states[0]
            processed += 1
            store.assert_claim(claim)
            if state.cleanup_status == "deleted":
                store.complete(record, claim)
            elif state.cleanup_status == "dead":
                store.dead_letter(
                    record,
                    record.item.model_copy(
                        update={
                            "ref": state,
                            "attempt": state.cleanup_attempts,
                            "error_code": state.cleanup_error_code,
                        }
                    ),
                    claim,
                )
            else:
                updated = record.item.model_copy(
                    update={
                        "ref": state,
                        "attempt": state.cleanup_attempts,
                        "next_at": state.cleanup_next_at or time.time() + 30.0,
                        "error_code": state.cleanup_error_code,
                    }
                )
                store.save(updated, expected_etag=record.etag)
        finally:
            store.release_claim(claim)
    return processed


def _lifecycle_filter_matches(filter_value: Any, prefix: str) -> bool:
    if not isinstance(filter_value, dict):
        return False
    conjunction = filter_value.get("And")
    if not isinstance(conjunction, dict) or conjunction.get("Prefix") != prefix:
        return False
    tags = conjunction.get("Tags")
    return isinstance(tags, list) and {
        (tag.get("Key"), tag.get("Value")) for tag in tags if isinstance(tag, dict)
    } == {(STAGING_TAG_KEY, STAGING_TAG_VALUE)}


def _lifecycle_rule_matches(
    rule: dict[str, Any],
    *,
    prefix: str,
    days: int,
    tagged: bool,
    require_abort: bool = True,
) -> bool:
    filter_value = rule.get("Filter")
    filter_matches = (
        _lifecycle_filter_matches(filter_value, prefix)
        if tagged
        else filter_value == {"Prefix": prefix}
    )
    abort_matches = (
        rule.get("AbortIncompleteMultipartUpload") == {"DaysAfterInitiation": days}
        if require_abort
        else "AbortIncompleteMultipartUpload" not in rule
    )
    return bool(
        rule.get("Status") == "Enabled"
        and filter_matches
        and rule.get("Expiration") == {"Days": days}
        and rule.get("NoncurrentVersionExpiration") == {"NoncurrentDays": days}
        and abort_matches
    )


def _multipart_lifecycle_rule_matches(
    rule: dict[str, Any], *, prefix: str, days: int
) -> bool:
    return bool(
        rule.get("Status") == "Enabled"
        and rule.get("Filter") == {"Prefix": prefix}
        and rule.get("AbortIncompleteMultipartUpload") == {"DaysAfterInitiation": days}
        and "Expiration" not in rule
        and "NoncurrentVersionExpiration" not in rule
    )


def _build_s3_client() -> S3ClientProtocol:
    from docling_serve.settings_views import current_staging_settings

    settings = current_staging_settings()
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    endpoint_url = settings.endpoint or None
    if endpoint_url is not None and not endpoint_url.startswith("https://"):
        if settings.verify_ssl:
            raise UploadStagingCapabilityError(
                "Staging endpoint must use https when TLS verification is enabled."
            )
    return cast(
        S3ClientProtocol,
        boto3.client(
            "s3",
            region_name=settings.region,
            endpoint_url=endpoint_url,
            verify=settings.verify_ssl,
            config=Config(
                connect_timeout=settings.io_timeout_seconds,
                read_timeout=settings.io_timeout_seconds,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        ),
    )


def build_upload_stager(*, client: S3ClientProtocol | None = None) -> S3UploadStager:
    from docling_serve.settings_views import current_staging_settings

    settings = current_staging_settings()
    if settings.mode == "disabled":
        raise UploadStagingDisabled("File uploads are disabled.")
    return S3UploadStager(
        client=client or _build_s3_client(),
        bucket=settings.bucket,
        bucket_id=_bucket_id(
            settings.bucket,
            settings.endpoint,
            settings.region,
        ),
        prefix=settings.key_prefix,
        endpoint=settings.endpoint,
        region=settings.region,
        retention_days=settings.retention_days,
        cleanup_retention_days=settings.cleanup_retention_days,
        dead_letter_retention_days=settings.dead_letter_retention_days,
        claim_retention_days=settings.claim_retention_days,
        claim_lease_seconds=settings.claim_lease_seconds,
        max_file_size=settings.max_file_size,
        kms_key_id=settings.kms_key_id or None,
        cleanup_retries=settings.cleanup_retries,
    )


def check_upload_staging_capability(*, force: bool = False) -> None:
    global _CAPABILITY_CACHE
    from docling_serve.settings_views import current_staging_settings

    settings = current_staging_settings()
    if settings.mode == "disabled":
        return
    now = time.monotonic()
    cache = _CAPABILITY_CACHE
    config_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "bucket": settings.bucket,
                "endpoint": settings.endpoint,
                "region": settings.region,
                "prefix": settings.key_prefix,
                "kms": settings.kms_key_id,
                "retention": settings.retention_days,
                "cleanup_retention": settings.cleanup_retention_days,
                "dead_retention": settings.dead_letter_retention_days,
                "claim_retention": settings.claim_retention_days,
                "claim_lease": settings.claim_lease_seconds,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if (
        not force
        and cache is not None
        and cache[1] == config_fingerprint
        and now - cache[0] < settings.probe_cache_seconds
    ):
        return
    try:
        build_upload_stager().check_capability()
    except UploadStagingError:
        raise
    except Exception as exc:
        raise UploadStagingCapabilityError(
            "Staging SDK or IAM capability probe failed."
        ) from exc
    _CAPABILITY_CACHE = (now, config_fingerprint)


@contextmanager
def materialize_staged_task(
    task: Task,
    *,
    stager: S3UploadStager | None = None,
) -> Iterator[Task]:
    refs = _parse_refs(task)
    if not refs:
        yield task
        return
    active_stager = stager or build_upload_stager()
    tenant_id = str(task.metadata.get("tenant_id") or "default")
    refs_by_id = {ref.upload_id: ref for ref in refs}
    if len(refs_by_id) != len(refs):
        raise StagedUploadTamperedError("Duplicate staged upload IDs.")

    streams: list[Any] = []
    sources: list[Any] = []
    identities: list[StagedPublicIdentity | None] = []
    seen: set[str] = set()
    try:
        for source in task.sources:
            if not is_staged_placeholder(source):
                sources.append(source)
                identities.append(None)
                continue
            assert isinstance(source, FileSource)
            upload_id = _placeholder_id(source)
            ref = refs_by_id.get(upload_id)
            if ref is None or upload_id in seen:
                raise StagedUploadTamperedError(
                    "Placeholder did not have exactly one private staged reference."
                )
            seen.add(upload_id)
            stream = active_stager.materialize(ref, tenant_id)
            streams.append(stream)
            sources.append(
                DocumentStream.model_construct(
                    name=ref.original_name,
                    stream=cast(BytesIO, stream),
                )
            )
            identities.append(
                StagedPublicIdentity(
                    original_name=ref.original_name,
                    content_type=ref.content_type,
                    original_uri=ref.original_uri,
                )
            )
        if seen != set(refs_by_id):
            raise StagedUploadTamperedError(
                "Private staged reference did not have exactly one placeholder."
            )
        worker_task = task.model_copy(
            update={
                "sources": sources,
                "metadata": {
                    key: value
                    for key, value in task.metadata.items()
                    if key != STAGED_UPLOAD_METADATA_KEY
                },
            }
        )
        with bind_staged_identities(tuple(identities)):
            yield worker_task
    finally:
        for stream in streams:
            stream.close()


def update_cleanup_metadata(task: Task, states: list[StagedUploadRef]) -> None:
    task.metadata[STAGED_UPLOAD_METADATA_KEY] = [
        state.model_dump(mode="json") for state in states
    ]


def cleanup_task_staged_uploads_sync(
    task: Task,
    *,
    stager: S3UploadStager | None = None,
) -> list[StagedUploadRef]:
    refs = _parse_refs(task)
    if not refs:
        return []
    active_stager = stager or build_upload_stager()
    tenant_id = str(task.metadata.get("tenant_id") or "default")
    for ref in refs:
        active_stager.validate_ref(ref, tenant_id)
    states = active_stager.cleanup_sync(refs)
    update_cleanup_metadata(task, states)
    failed_states = [state for state in states if state.cleanup_status != "deleted"]
    if failed_states:
        cleanup_store = S3CleanupStore(active_stager)
        try:
            for state in failed_states:
                cleanup_store.enqueue_ref(state)
        except Exception as exc:
            raise StagedUploadCleanupError(
                "Durable cleanup queue write failed; lifecycle remains active."
            ) from exc
        raise StagedUploadCleanupError(
            "One or more staged objects could not be deleted after retries."
        )
    return states


async def cleanup_task_staged_uploads(
    task: Task,
    *,
    stager: S3UploadStager | None = None,
) -> list[StagedUploadRef]:
    return await asyncio.to_thread(
        cleanup_task_staged_uploads_sync,
        task,
        stager=stager,
    )


def persist_cleanup_state(
    redis_client: Any,
    *,
    task_id: str,
    states: list[StagedUploadRef],
    ttl_seconds: int,
) -> None:
    payload = json.dumps(
        {
            "task_id": task_id,
            "objects": [
                {
                    "upload_id": state.upload_id,
                    "status": state.cleanup_status,
                    "attempts": state.cleanup_attempts,
                    "nextAt": state.cleanup_next_at,
                    "errorCode": state.cleanup_error_code,
                }
                for state in states
            ],
        },
        sort_keys=True,
    )
    redis_client.setex(
        f"{STAGING_CLEANUP_KEY_PREFIX}{task_id}",
        ttl_seconds,
        payload,
    )


def sanitize_task_for_public(task: Task) -> Task:
    """Remove private staging metadata and placeholders from public task objects."""

    metadata = {
        key: value
        for key, value in task.metadata.items()
        if key != STAGED_UPLOAD_METADATA_KEY
    }
    sources = [source for source in task.sources if not is_staged_placeholder(source)]
    return task.model_copy(update={"metadata": metadata, "sources": sources})


def redact_sensitive_text(value: str) -> str:
    """Remove URL query/fragment data and internal staging identifiers."""

    def strip_url_secrets(match: re.Match[str]) -> str:
        parsed = urlsplit(match.group(0))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    text = re.sub(r"https?://[^\s]+", strip_url_secrets, value)
    text = _AUTHORIZATION_PATTERN.sub("[redacted]", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub("[redacted]", text)
    text = _AWS_ASSIGNMENT_PATTERN.sub("[redacted]", text)
    text = _JWT_PATTERN.sub("[redacted]", text)
    text = _AWS_ACCESS_KEY_PATTERN.sub("[redacted]", text)
    text = re.sub(
        rf"{re.escape(STAGED_PLACEHOLDER_PREFIX)}[0-9a-f-]+",
        "[staged-upload]",
        text,
    )
    text = re.sub(r"docling-staging/v1/[0-9a-f/]+", "[staged-object]", text)
    return text


def lifecycle_days_for_seconds(seconds: int) -> int:
    return max(1, math.ceil(seconds / 86400))
