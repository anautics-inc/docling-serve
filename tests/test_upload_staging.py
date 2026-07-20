import asyncio
import copy
import io
import json
import logging
import pickle
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from docling.datamodel.base_models import DocumentStream
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.sources import FileSource
from docling.datamodel.service.targets import InBodyTarget
from docling_jobkit.datamodel.task import Task
from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestrator

from docling_serve.logging_config import JSONLogFormatter
from docling_serve.rq_orchestrator import RedactedTaskQueue
from docling_serve.settings import DoclingServeSettings
from docling_serve.upload_staging import (
    STAGED_PLACEHOLDER_PREFIX,
    STAGED_UPLOAD_METADATA_KEY,
    STAGING_CLEANUP_CLAIM_PREFIX,
    STAGING_CLEANUP_DEAD_PREFIX,
    STAGING_CLEANUP_QUEUE_PREFIX,
    CleanupQueueItem,
    S3CleanupStore,
    S3UploadStager,
    StagedUploadCleanupError,
    StagedUploadRef,
    StagedUploadTamperedError,
    UploadStagingCapabilityError,
    UploadStagingDisabled,
    UploadStagingInputError,
    build_upload_stager,
    cleanup_task_staged_uploads_sync,
    materialize_staged_task,
    persist_cleanup_state,
    reconcile_cleanup_once,
    redact_sensitive_text,
    sanitize_task_for_public,
)


@pytest.mark.parametrize(
    "hostile_mime",
    [
        "text/plain; charset=utf-8",
        " text/plain",
        "text/plain ",
        "text /plain",
        "https://objects.example/file",
        "text/plain?token=value",
        "text/plain#fragment",
        "text/plain\r\nAuthorization: Bearer value",
        "Bearer token/value",
        "text",
        "text/",
        f"text/{'a' * 128}",
    ],
)
@pytest.mark.asyncio
async def test_hostile_media_types_are_rejected_before_object_write(hostile_mime):
    client = _S3()
    with pytest.raises(UploadStagingInputError):
        await _stager(client).stage(
            payload=b"bytes",
            filename="report.doc",
            content_type=hostile_mime,
            tenant_id="tenant-a",
        )
    assert client.put_calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("original_uri", "https://user:password@example.test/report.doc"),
        ("original_uri", "https://example.test/report.doc?X-Amz-Signature=value"),
        ("original_uri", "https://example.test/report.doc?%58-Amz-Credential=value"),
        ("original_uri", "https://example.test/report.doc?token=value"),
        ("original_uri", "https://example.test/eyJabc.def.ghi"),
        ("version_id", "session-token=value"),
        ("version_id", "Authorization: Bearer abc.def"),
        ("version_id", "AKIAIOSFODNN7EXAMPLE"),
        ("original_name", "Authorization=Bearer abc.def.doc"),
        ("original_name", "eyJheader.payload.signature.doc"),
        ("content_type", "Bearer token/value"),
        ("content_type", "application/eyJheader.payload.signature"),
    ],
)
def test_all_client_string_fields_reject_bearer_patterns(field, value):
    data = {
        "upload_id": "1" * 32,
        "bucket_id": "b" * 64,
        "key": f"docling-staging/v1/{'a' * 64}/{'1' * 32}",
        "checksum_sha256": "c" * 64,
        "size_bytes": 4,
        "content_type": "application/octet-stream",
        "original_name": "report.doc",
        "tenant_hash": "a" * 64,
    }
    data[field] = value
    with pytest.raises(ValueError):
        StagedUploadRef.model_validate(data)


@pytest.mark.parametrize(
    "filename",
    [
        "secretary-notes.doc",
        "credentialing-guide.doc",
        "password-policy.doc",
        "token-economics.doc",
        "X-Amz-Credential.doc",
        " report with spaces .doc ",
    ],
)
def test_filename_validation_is_path_aware_not_secret_substring_based(filename):
    ref = StagedUploadRef(
        upload_id="1" * 32,
        bucket_id="b" * 64,
        key=f"docling-staging/v1/{'a' * 64}/{'1' * 32}",
        checksum_sha256="c" * 64,
        size_bytes=4,
        content_type="application/x-api-key",
        original_name=filename,
        tenant_hash="a" * 64,
    )
    assert ref.original_name == filename
    assert ref.content_type == "application/x-api-key"


def test_uri_sanitizes_benign_query_and_fragment():
    ref = StagedUploadRef(
        upload_id="1" * 32,
        bucket_id="b" * 64,
        key=f"docling-staging/v1/{'a' * 64}/{'1' * 32}",
        checksum_sha256="c" * 64,
        size_bytes=4,
        content_type="application/octet-stream",
        original_name="secretary-notes.doc",
        original_uri="https://example.test/report.doc?view=compact#page=2",
        tenant_hash="a" * 64,
        version_id="credentialing-password-policy",
        cleanup_error_code="PasswordPolicy",
    )
    assert ref.original_uri == "https://example.test/report.doc"
    assert ref.version_id == "credentialing-password-policy"
    assert ref.cleanup_error_code == "PasswordPolicy"


class _Body(io.BytesIO):
    def close(self):
        self.was_closed = True
        super().close()


class _PreconditionFailed(Exception):
    response = {
        "Error": {"Code": "PreconditionFailed"},
        "ResponseMetadata": {"HTTPStatusCode": 412},
    }


class _S3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.delete_calls = []
        self.delete_errors = []
        self.list_calls = []
        self.etag_counter = 0
        self.lifecycle = {
            "Rules": [
                {
                    "ID": "docling-staging-expiration",
                    "Status": "Enabled",
                    "Filter": {
                        "And": {
                            "Prefix": "docling-staging/v1/",
                            "Tags": [{"Key": "docling-staging", "Value": "true"}],
                        }
                    },
                    "Expiration": {"Days": 1},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                },
                {
                    "ID": "docling-staging-multipart-abort",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "docling-staging/v1/"},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                },
                {
                    "ID": "docling-staging-cleanup-expiration",
                    "Status": "Enabled",
                    "Filter": {"Prefix": STAGING_CLEANUP_QUEUE_PREFIX},
                    "Expiration": {"Days": 7},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                },
                {
                    "ID": "docling-staging-dead-letter-expiration",
                    "Status": "Enabled",
                    "Filter": {"Prefix": STAGING_CLEANUP_DEAD_PREFIX},
                    "Expiration": {"Days": 30},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 30},
                },
                {
                    "ID": "docling-staging-claim-expiration",
                    "Status": "Enabled",
                    "Filter": {"Prefix": STAGING_CLEANUP_CLAIM_PREFIX},
                    "Expiration": {"Days": 1},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                },
            ]
        }

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        current = self.objects.get(kwargs["Key"])
        if kwargs.get("IfNoneMatch") == "*" and current is not None:
            raise _PreconditionFailed
        if "IfMatch" in kwargs and (
            current is None or current["etag"] != kwargs["IfMatch"]
        ):
            raise _PreconditionFailed
        self.etag_counter += 1
        etag = f'"etag-{self.etag_counter}"'
        self.objects[kwargs["Key"]] = {
            "payload": bytes(kwargs["Body"]),
            "metadata": kwargs["Metadata"],
            "content_type": kwargs["ContentType"],
            "encryption": kwargs["ServerSideEncryption"],
            "kms": kwargs.get("SSEKMSKeyId"),
            "etag": etag,
        }
        return {"VersionId": "v1", "ETag": etag}

    def head_object(self, *, Key, **kwargs):
        obj = self.objects[Key]
        if "IfMatch" in kwargs and kwargs["IfMatch"] != obj["etag"]:
            raise _PreconditionFailed
        return {
            "ContentLength": len(obj["payload"]),
            "ContentType": obj["content_type"],
            "Metadata": obj["metadata"],
            "ServerSideEncryption": obj["encryption"],
            "SSEKMSKeyId": obj["kms"],
            "ETag": obj["etag"],
        }

    def get_object(self, *, Key, **kwargs):
        obj = self.objects[Key]
        if "IfMatch" in kwargs and kwargs["IfMatch"] != obj["etag"]:
            raise _PreconditionFailed
        return {"Body": _Body(obj["payload"]), "ETag": obj["etag"]}

    def delete_objects(self, **kwargs):
        self.delete_calls.append(kwargs)
        errors = self.delete_errors.pop(0) if self.delete_errors else []
        errored = {item["Key"] for item in errors}
        for item in kwargs["Delete"]["Objects"]:
            if item["Key"] not in errored:
                self.objects.pop(item["Key"], None)
        return {"Errors": errors}

    def delete_object(self, *, Key, **kwargs):
        obj = self.objects.get(Key)
        if "IfMatch" in kwargs and (obj is None or kwargs["IfMatch"] != obj["etag"]):
            raise _PreconditionFailed
        self.objects.pop(Key, None)
        return {}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        del kwargs
        return self.lifecycle

    def list_objects_v2(self, *, Prefix, MaxKeys, **kwargs):
        del kwargs
        self.list_calls.append({"Prefix": Prefix, "MaxKeys": MaxKeys})
        keys = sorted(key for key in self.objects if key.startswith(Prefix))[:MaxKeys]
        return {
            "Contents": [
                {"Key": key, "ETag": self.objects[key]["etag"]} for key in keys
            ]
        }


def _stager(client=None, *, kms_key_id=None, cleanup_retries=2):
    return S3UploadStager(
        client=client or _S3(),
        bucket="uploads",
        bucket_id="b" * 64,
        prefix="docling-staging/v1/",
        endpoint="",
        region="us-east-1",
        retention_days=1,
        cleanup_retention_days=7,
        dead_letter_retention_days=30,
        claim_retention_days=1,
        claim_lease_seconds=60,
        max_file_size=1024,
        kms_key_id=kms_key_id,
        cleanup_retries=cleanup_retries,
    )


def _task(staged, tenant_id="tenant-a"):
    return Task(
        task_id="serialized-upload",
        sources=[staged.source],
        target=InBodyTarget(),
        convert_options=ConvertDocumentsOptions(),
        metadata={
            "tenant_id": tenant_id,
            STAGED_UPLOAD_METADATA_KEY: [staged.ref.model_dump(mode="json")],
        },
    )


@pytest.mark.asyncio
async def test_task_and_rq_ray_payloads_have_only_nonsecret_placeholder_and_ref():
    client = _S3()
    stager = _stager(client)
    staged = await stager.stage(
        payload=b"legacy-binary",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    assert isinstance(staged.source, FileSource)
    assert staged.source.base64_string == ""
    assert staged.source.filename.startswith(STAGED_PLACEHOLDER_PREFIX)
    assert client.put_calls[0]["ServerSideEncryption"] == "AES256"
    assert client.put_calls[0]["Tagging"] == "docling-staging=true"
    assert "tenant-a" not in client.put_calls[0]["Key"]
    assert not hasattr(client, "generate_presigned_url")

    task = _task(staged)
    json_payload = task.model_dump_json()
    restored = Task.model_validate_json(json_payload)
    rq_payload = task.model_dump(mode="json", serialize_as_any=True)
    rq_round_trip = pickle.loads(pickle.dumps(rq_payload))
    ray_round_trip = Task.model_validate_json(task.model_dump_json())
    for serialized in (
        json_payload,
        json.dumps(rq_round_trip),
        ray_round_trip.model_dump_json(),
    ):
        lowered = serialized.lower()
        assert "x-amz-" not in lowered
        assert "signature=" not in lowered
        assert "secret" not in lowered
        assert "legacy-binary" not in lowered
        assert "http://" not in lowered
        assert "https://" not in lowered
        for bearer_pattern in (
            "authorization",
            "bearer",
            "credential",
            "password",
            "x-amz-signature",
            "x-amz-security-token",
        ):
            assert bearer_pattern not in lowered
    assert isinstance(restored.sources[0], FileSource)
    assert Task.model_validate(rq_round_trip).sources[0].base64_string == ""


@pytest.mark.asyncio
async def test_worker_materializes_by_iam_validates_checksum_and_restores_identity():
    client = _S3()
    stager = _stager(client)
    staged = await stager.stage(
        payload=b"legacy-binary",
        filename="report.doc",
        content_type="application/x-custom-office",
        tenant_id="tenant-a",
    )
    task = _task(staged)
    with materialize_staged_task(task, stager=stager) as worker_task:
        assert isinstance(worker_task.sources[0], DocumentStream)
        assert worker_task.sources[0].name == "report.doc"
        assert worker_task.sources[0].stream.read() == b"legacy-binary"
        assert STAGED_UPLOAD_METADATA_KEY not in worker_task.metadata

    tampered = staged.ref.model_copy(update={"key": "other/key"})
    bad_task = task.model_copy(
        update={
            "metadata": {
                **task.metadata,
                STAGED_UPLOAD_METADATA_KEY: [tampered.model_dump(mode="json")],
            }
        }
    )
    with pytest.raises(StagedUploadTamperedError):
        with materialize_staged_task(bad_task, stager=stager):
            pass

    client.objects[staged.ref.key]["payload"] = b"changed"
    with pytest.raises(StagedUploadTamperedError):
        with materialize_staged_task(task, stager=stager):
            pass


@pytest.mark.asyncio
async def test_media_type_is_normalized_before_storage_and_serialization():
    client = _S3()
    staged = await _stager(client).stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="Application/Octet-Stream",
        tenant_id="tenant-a",
    )
    assert staged.ref.content_type == "application/octet-stream"
    assert client.put_calls[0]["ContentType"] == "application/octet-stream"
    assert "Application/Octet-Stream" not in _task(staged).model_dump_json()


def test_materialization_closes_body_and_stream_on_every_failure(monkeypatch):
    from docling_serve import upload_staging as module

    class ReadFailure(BaseException):
        pass

    streams = []
    bodies = []
    fd_dir = Path("/proc/self/fd")
    fd_count_before = len(list(fd_dir.iterdir())) if fd_dir.exists() else None

    class FailingBody(_Body):
        def read(self, amount=-1):
            del amount
            raise ReadFailure("cancelled")

    client = _S3()
    stager = _stager(client)
    staged = asyncio.run(
        stager.stage(
            payload=b"bytes",
            filename="report.doc",
            content_type="application/octet-stream",
            tenant_id="tenant-a",
        )
    )

    original_bytes_io = module.BytesIO

    def make_stream(*args, **kwargs):
        del args, kwargs
        stream = original_bytes_io()
        streams.append(stream)
        return stream

    def get_object(**kwargs):
        del kwargs
        body = FailingBody(b"bytes")
        bodies.append(body)
        return {"Body": body}

    monkeypatch.setattr(module, "BytesIO", make_stream)
    client.get_object = get_object
    for _ in range(25):
        with pytest.raises(ReadFailure):
            stager.materialize(staged.ref, "tenant-a")
    assert len(streams) == len(bodies) == 25
    assert all(stream.closed for stream in streams)
    assert all(body.closed for body in bodies)
    if fd_count_before is not None:
        assert len(list(fd_dir.iterdir())) <= fd_count_before + 1


def test_materialization_closes_stream_after_checksum_failure(monkeypatch):
    from docling_serve import upload_staging as module

    client = _S3()
    stager = _stager(client)
    staged = asyncio.run(
        stager.stage(
            payload=b"bytes",
            filename="report.doc",
            content_type="application/octet-stream",
            tenant_id="tenant-a",
        )
    )
    client.objects[staged.ref.key]["payload"] = b"other"
    streams = []

    def make_stream(*args, **kwargs):
        del args, kwargs
        stream = io.BytesIO()
        streams.append(stream)
        return stream

    monkeypatch.setattr(module, "BytesIO", make_stream)
    for _ in range(25):
        with pytest.raises(StagedUploadTamperedError):
            stager.materialize(staged.ref, "tenant-a")
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_kms_and_partial_delete_errors_are_validated_and_retried():
    client = _S3()
    stager = _stager(client, kms_key_id="kms-key", cleanup_retries=1)
    staged = await stager.stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    assert client.put_calls[0]["ServerSideEncryption"] == "aws:kms"
    assert client.put_calls[0]["SSEKMSKeyId"] == "kms-key"
    client.delete_errors = [[{"Key": staged.ref.key, "Code": "SlowDown"}], []]
    states = await stager.cleanup([staged.ref])
    assert states[0].cleanup_status == "deleted"
    assert len(client.delete_calls) == 2

    staged2 = await stager.stage(
        payload=b"bytes2",
        filename="report2.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    client.delete_errors = [
        [{"Key": staged2.ref.key, "Code": "AccessDenied"}],
    ]
    states = await stager.cleanup([staged2.ref])
    assert states[0].cleanup_status == "dead"
    assert states[0].cleanup_error_code == "AccessDenied"

    delete_count = len(client.delete_calls)
    forged = staged2.ref.model_copy(
        update={"key": f"docling-staging/v1/{'f' * 64}/{'2' * 32}"}
    )
    with pytest.raises(StagedUploadTamperedError):
        await stager.cleanup([forged])
    assert len(client.delete_calls) == delete_count


@pytest.mark.asyncio
async def test_cleanup_retry_survives_restart_and_reconciles_without_polling():
    client = _S3()
    stager = _stager(client, cleanup_retries=0)
    staged = await stager.stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    task = _task(staged)
    client.delete_errors = [[{"Key": staged.ref.key, "Code": "SlowDown"}]]
    with pytest.raises(StagedUploadCleanupError):
        cleanup_task_staged_uploads_sync(task, stager=stager)

    queue_keys = [
        key for key in client.objects if key.startswith(STAGING_CLEANUP_QUEUE_PREFIX)
    ]
    assert len(queue_keys) == 1
    queue_put = next(call for call in client.put_calls if call["Key"] == queue_keys[0])
    assert queue_put["ServerSideEncryption"] == "AES256"
    assert task.metadata[STAGED_UPLOAD_METADATA_KEY][0]["cleanup_attempts"] == 1
    assert task.metadata[STAGED_UPLOAD_METADATA_KEY][0]["cleanup_next_at"] > 0
    assert (
        task.metadata[STAGED_UPLOAD_METADATA_KEY][0]["cleanup_error_code"] == "SlowDown"
    )

    restarted_stager = _stager(client, cleanup_retries=0)
    processed = reconcile_cleanup_once(
        restarted_stager,
        now=time.time() + 7200,
        max_items=4,
    )
    assert processed == 1
    assert staged.ref.key not in client.objects
    assert not any(
        key.startswith(STAGING_CLEANUP_QUEUE_PREFIX) for key in client.objects
    )


@pytest.mark.asyncio
async def test_duplicate_replicas_claim_once_and_delete_idempotently():
    client = _S3()
    stager = _stager(client, cleanup_retries=0)
    staged = await stager.stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    task = _task(staged)
    client.delete_errors = [[{"Key": staged.ref.key, "Code": "SlowDown"}]]
    with pytest.raises(StagedUploadCleanupError):
        cleanup_task_staged_uploads_sync(task, stager=stager)
    client.delete_calls.clear()
    reconcile_now = time.time() + 7200

    nested_results = []
    original_cleanup = stager.cleanup_sync

    def cleanup_with_competing_replica(refs):
        nested_results.append(
            reconcile_cleanup_once(
                stager,
                now=reconcile_now,
                max_items=1,
                owner_id="2" * 32,
            )
        )
        return original_cleanup(refs)

    stager.cleanup_sync = cleanup_with_competing_replica
    assert (
        reconcile_cleanup_once(
            stager,
            now=reconcile_now,
            max_items=1,
            owner_id="1" * 32,
        )
        == 1
    )
    assert nested_results == [0]
    assert len(client.delete_calls) == 1
    assert staged.ref.key not in client.objects
    assert not any(
        key.startswith((STAGING_CLEANUP_QUEUE_PREFIX, STAGING_CLEANUP_CLAIM_PREFIX))
        for key in client.objects
    )


@pytest.mark.asyncio
async def test_expired_claim_is_etag_fenced_and_reclaimed_after_crash():
    client = _S3()
    stager = _stager(client, cleanup_retries=0)
    staged = await stager.stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    retry_ref = staged.ref.model_copy(
        update={
            "cleanup_status": "retry",
            "cleanup_next_at": time.time(),
            "cleanup_error_code": "SlowDown",
        }
    )
    store = S3CleanupStore(stager)
    store.enqueue_ref(retry_ref)
    record = store.due(now=time.time() + 1, limit=1)[0]
    started = time.time()
    first = store.claim(record, owner_id="1" * 32, now=started)
    assert first is not None
    assert store.claim(record, owner_id="2" * 32, now=started + 1) is None

    reclaimed = store.claim(
        record,
        owner_id="2" * 32,
        now=started + stager.claim_lease_seconds + 1,
    )
    assert reclaimed is not None
    assert reclaimed.etag != first.etag
    with pytest.raises(_PreconditionFailed):
        store.complete(record, first)
    store.complete(record, reclaimed)
    store.release_claim(first)
    store.release_claim(reclaimed)
    assert record.key not in client.objects


@pytest.mark.asyncio
async def test_due_cleanup_scan_paginates_past_not_yet_due_uuid_keys():
    class _PaginatedS3(_S3):
        def list_objects_v2(self, *, Prefix, MaxKeys, ContinuationToken=None, **kwargs):
            del MaxKeys, kwargs
            keys = sorted(key for key in self.objects if key.startswith(Prefix))
            index = int(ContinuationToken or "0")
            page = keys[index : index + 1]
            next_index = index + len(page)
            return {
                "Contents": [
                    {"Key": key, "ETag": self.objects[key]["etag"]} for key in page
                ],
                "IsTruncated": next_index < len(keys),
                **(
                    {"NextContinuationToken": str(next_index)}
                    if next_index < len(keys)
                    else {}
                ),
            }

    client = _PaginatedS3()
    stager = _stager(client)
    staged = await stager.stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    store = S3CleanupStore(stager)
    now = time.time()
    store.save(
        CleanupQueueItem(
            queue_id="0" * 32,
            ref=staged.ref,
            next_at=now + 3600,
        ),
        create=True,
    )
    due_item = CleanupQueueItem(
        queue_id="f" * 32,
        ref=staged.ref,
        next_at=now - 1,
    )
    store.save(due_item, create=True)

    records = store.due(now=now, limit=1)
    assert [record.item.queue_id for record in records] == [due_item.queue_id]


@pytest.mark.asyncio
async def test_permanent_cleanup_failure_is_dead_lettered_without_key_in_logs(caplog):
    client = _S3()
    stager = _stager(client, cleanup_retries=2)
    staged = await stager.stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    task = _task(staged)
    client.delete_errors = [[{"Key": staged.ref.key, "Code": "AccessDenied"}]]
    with caplog.at_level(logging.ERROR), pytest.raises(StagedUploadCleanupError):
        cleanup_task_staged_uploads_sync(task, stager=stager)
    dead_keys = [
        key for key in client.objects if key.startswith(STAGING_CLEANUP_DEAD_PREFIX)
    ]
    assert len(dead_keys) == 1
    assert staged.ref.key not in caplog.text
    assert staged.ref.upload_id not in caplog.text
    assert task.metadata[STAGED_UPLOAD_METADATA_KEY][0]["cleanup_status"] == "dead"
    assert (
        task.metadata[STAGED_UPLOAD_METADATA_KEY][0]["cleanup_error_code"]
        == "AccessDenied"
    )


@pytest.mark.parametrize(
    "lifecycle",
    [
        {"Rules": []},
        {
            "Rules": [
                {
                    "ID": "docling-staging-expiration",
                    "Status": "Disabled",
                    "Filter": {
                        "And": {
                            "Prefix": "docling-staging/v1/",
                            "Tags": [{"Key": "docling-staging", "Value": "true"}],
                        }
                    },
                    "Expiration": {"Days": 1},
                }
            ]
        },
        {
            "Rules": [
                {
                    "ID": "docling-staging-expiration",
                    "Status": "Enabled",
                    "Filter": {
                        "And": {
                            "Prefix": "wrong/",
                            "Tags": [{"Key": "docling-staging", "Value": "true"}],
                        }
                    },
                    "Expiration": {"Days": 1},
                }
            ]
        },
    ],
)
def test_readiness_rejects_missing_disabled_or_wrong_lifecycle(lifecycle):
    client = _S3()
    client.lifecycle = lifecycle
    with pytest.raises(UploadStagingCapabilityError):
        _stager(client).check_capability()


@pytest.mark.parametrize(
    "rule_id",
    [
        "docling-staging-expiration",
        "docling-staging-multipart-abort",
        "docling-staging-cleanup-expiration",
        "docling-staging-dead-letter-expiration",
        "docling-staging-claim-expiration",
    ],
)
def test_readiness_requires_every_exact_lifecycle_rule(rule_id):
    client = _S3()
    client.lifecycle = copy.deepcopy(client.lifecycle)
    client.lifecycle["Rules"] = [
        rule for rule in client.lifecycle["Rules"] if rule["ID"] != rule_id
    ]
    with pytest.raises(UploadStagingCapabilityError):
        _stager(client).check_capability()


def test_readiness_rejects_overlong_dead_letter_retention():
    client = _S3()
    client.lifecycle = copy.deepcopy(client.lifecycle)
    dead_rule = next(
        rule
        for rule in client.lifecycle["Rules"]
        if rule["ID"] == "docling-staging-dead-letter-expiration"
    )
    dead_rule["Expiration"]["Days"] = 31
    with pytest.raises(UploadStagingCapabilityError):
        _stager(client).check_capability()


def test_readiness_runs_bounded_put_head_get_delete_canary():
    client = _S3()
    _stager(client).check_capability()
    assert len(client.put_calls) == 3
    assert client.put_calls[0]["Body"] == b"docling-staging-canary"
    assert client.put_calls[1]["Key"].startswith(STAGING_CLEANUP_QUEUE_PREFIX)
    assert client.put_calls[1]["ServerSideEncryption"] == "AES256"
    assert client.put_calls[2]["Key"].startswith(STAGING_CLEANUP_CLAIM_PREFIX)
    assert client.put_calls[2]["IfNoneMatch"] == "*"
    assert len(client.list_calls) == 1
    assert client.list_calls[0]["Prefix"].startswith(STAGING_CLEANUP_QUEUE_PREFIX)
    assert client.list_calls[0]["MaxKeys"] == 4
    assert client.delete_calls
    assert client.objects == {}


def test_readiness_rejects_unreachable_or_wrong_encryption():
    client = _S3()
    client.get_bucket_lifecycle_configuration = lambda **kwargs: (_ for _ in ()).throw(
        OSError("unreachable")
    )
    with pytest.raises(UploadStagingCapabilityError):
        _stager(client).check_capability()

    client = _S3()
    original_head = client.head_object

    def wrong_encryption(**kwargs):
        response = original_head(**kwargs)
        response["ServerSideEncryption"] = "aws:kms"
        response["SSEKMSKeyId"] = "wrong"
        return response

    client.head_object = wrong_encryption
    with pytest.raises(UploadStagingCapabilityError):
        _stager(client).check_capability()


def test_required_settings_fail_closed_and_disabled_mode_is_explicit(monkeypatch):
    with pytest.raises(ValidationError):
        DoclingServeSettings(upload_staging_dead_letter_retention_days=91)
    with pytest.raises(ValidationError):
        DoclingServeSettings(
            upload_staging_mode="required",
            upload_staging_bucket="",
            upload_staging_region="",
        )
    with pytest.raises(ValidationError):
        DoclingServeSettings(
            upload_staging_mode="required",
            upload_staging_bucket="bucket",
            upload_staging_region="us-east-1",
            upload_staging_verify_ssl=False,
        )

    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(docling_serve_settings, "upload_staging_mode", "disabled")
    with pytest.raises(UploadStagingDisabled):
        build_upload_stager()


def test_rq_worker_staging_probe_runs_before_worker_init(tmp_path, monkeypatch):
    from docling_jobkit.orchestrators.rq.worker import CustomRQWorker

    from docling_serve import rq_worker_instrumented as rq_module

    monkeypatch.setattr(
        rq_module.docling_serve_settings, "legacy_office_enabled", False
    )
    monkeypatch.setattr(
        rq_module.docling_serve_settings, "upload_staging_mode", "required"
    )
    with (
        patch.object(CustomRQWorker, "__init__") as base_init,
        patch.object(
            rq_module,
            "check_upload_staging_capability",
            side_effect=UploadStagingCapabilityError("unavailable"),
        ),
        pytest.raises(UploadStagingCapabilityError),
    ):
        rq_module.InstrumentedRQWorker(
            [],
            orchestrator_config=SimpleNamespace(),
            cm_config=SimpleNamespace(),
            scratch_dir=tmp_path,
        )
    base_init.assert_not_called()


def test_every_ray_converter_replica_forces_staging_probe(monkeypatch):
    from docling_serve import ray_legacy

    replica_class = ray_legacy.LegacyOfficeRayConverterDeployment.func_or_class
    monkeypatch.setattr(
        ray_legacy.docling_serve_settings, "legacy_office_enabled", False
    )
    monkeypatch.setattr(
        ray_legacy.docling_serve_settings, "upload_staging_mode", "required"
    )
    monkeypatch.setattr(
        ray_legacy,
        "check_upload_staging_capability",
        lambda **kwargs: (_ for _ in ()).throw(
            UploadStagingCapabilityError("unavailable")
        ),
    )
    instance = replica_class.__new__(replica_class)
    with pytest.raises(UploadStagingCapabilityError):
        replica_class.__init__(instance, SimpleNamespace(), SimpleNamespace())


@pytest.mark.asyncio
async def test_real_rq_metadata_storage_contains_no_bearer_url():
    client = _S3()
    staged = await _stager(client).stage(
        payload=b"bytes",
        filename="report.doc",
        content_type="application/octet-stream",
        tenant_id="tenant-a",
    )
    task = _task(staged)

    class _Redis:
        async def set(self, key, payload, *, ex):
            self.value = (key, payload, ex)

    redis = _Redis()
    fake_orchestrator = SimpleNamespace(
        _async_redis_conn=redis,
        config=SimpleNamespace(results_ttl=60),
    )
    await RQOrchestrator._store_task_in_redis(fake_orchestrator, task)
    payload = redis.value[1]
    assert STAGED_UPLOAD_METADATA_KEY in payload
    assert "X-Amz-" not in payload
    assert "signature=" not in payload.lower()
    assert "http://" not in payload
    assert "https://" not in payload


def test_rq_job_description_never_renders_private_task_arguments():
    from rq import Queue

    queue = RedactedTaskQueue.__new__(RedactedTaskQueue)
    with patch.object(Queue, "enqueue", return_value="job") as base_enqueue:
        result = queue.enqueue(
            "docling_serve.rq_job_wrapper.instrumented_docling_task",
            kwargs={
                "task_data": {
                    STAGED_UPLOAD_METADATA_KEY: [
                        {"key": f"docling-staging/v1/{'a' * 64}/opaque"}
                    ]
                }
            },
            job_id="task-1",
        )
    assert result == "job"
    description = base_enqueue.call_args.kwargs["description"]
    assert description == "docling task task-1"
    assert "docling-staging" not in description


def test_cleanup_state_is_persistable_and_public_boundaries_are_redacted():
    ref = StagedUploadRef(
        upload_id="1" * 32,
        bucket_id="b" * 64,
        key=f"docling-staging/v1/{'a' * 64}/{'1' * 32}",
        checksum_sha256="c" * 64,
        size_bytes=4,
        content_type="application/octet-stream",
        original_name="report.doc",
        tenant_hash="a" * 64,
        cleanup_status="retry",
        cleanup_attempts=3,
    )
    redis = SimpleNamespace(setex=lambda *args: setattr(redis, "call", args))
    persist_cleanup_state(
        redis,
        task_id="task-1",
        states=[ref],
        ttl_seconds=60,
    )
    assert ref.key not in redis.call[2]
    assert "retry" in redis.call[2]

    task = Task(
        task_id="task-1",
        sources=[
            FileSource(
                filename=f"{STAGED_PLACEHOLDER_PREFIX}{ref.upload_id}",
                base64_string="",
            )
        ],
        metadata={STAGED_UPLOAD_METADATA_KEY: [ref.model_dump(mode="json")]},
    )
    public = sanitize_task_for_public(task)
    serialized = public.model_dump_json()
    assert STAGED_UPLOAD_METADATA_KEY not in serialized
    assert STAGED_PLACEHOLDER_PREFIX not in serialized
    redacted = redact_sensitive_text(
        "https://example.test/file?X-Amz-Signature=secret "
        f"{ref.key} {STAGED_PLACEHOLDER_PREFIX}{ref.upload_id}"
    )
    assert "secret" not in redacted
    assert ref.key not in redacted
    assert ref.upload_id not in redacted

    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        (
            "failed https://example.test/file?arbitrary-secret=value "
            f"{ref.key} {STAGED_PLACEHOLDER_PREFIX}{ref.upload_id}"
        ),
        (),
        None,
    )
    log_json = JSONLogFormatter().format(record)
    assert "arbitrary-secret" not in log_json
    assert ref.key not in log_json
    assert ref.upload_id not in log_json
