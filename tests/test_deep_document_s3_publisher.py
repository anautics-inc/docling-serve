from __future__ import annotations

from pathlib import Path

import pytest

from docling_serve.deep_document import s3_publisher
from docling_serve.deep_document.s3_publisher import DeepBucketNotAllowed
from docling_serve.settings import docling_serve_settings


def test_empty_request_bucket_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "server-bkt")
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_allowed_buckets", None)
    # No exception: falls back to the server default.
    s3_publisher.ensure_bucket_allowed("")


def test_arbitrary_request_bucket_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "server-bkt")
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_allowed_buckets", None)
    with pytest.raises(DeepBucketNotAllowed):
        s3_publisher.ensure_bucket_allowed("attacker-owned-bucket")


def test_server_default_bucket_is_always_allowed(monkeypatch) -> None:
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "server-bkt")
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_allowed_buckets", None)
    s3_publisher.ensure_bucket_allowed("server-bkt")


def test_allow_listed_bucket_is_permitted(monkeypatch) -> None:
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "server-bkt")
    monkeypatch.setattr(
        docling_serve_settings, "deep_document_s3_allowed_buckets", ["tenant-bkt"]
    )
    s3_publisher.ensure_bucket_allowed("tenant-bkt")
    with pytest.raises(DeepBucketNotAllowed):
        s3_publisher.ensure_bucket_allowed("other-bkt")


def test_normalize_prefix_strips_traversal_segments() -> None:
    assert s3_publisher.normalize_prefix("../../etc/../secret//x") == "etc/secret/x"
    assert s3_publisher.normalize_prefix("..") == "docling"


def test_resolve_deep_target_rejects_unallowed_request_bucket(monkeypatch) -> None:
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "server-bkt")
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_allowed_buckets", None)
    with pytest.raises(DeepBucketNotAllowed):
        s3_publisher.resolve_deep_target(
            task_id="t1", request_bucket="attacker-owned-bucket"
        )


def test_default_bucket_falls_back_to_app_s3_bucket(monkeypatch) -> None:
    monkeypatch.setattr(docling_serve_settings, "deep_document_s3_bucket", "")
    monkeypatch.setenv("S3_BUCKET_NAME", "captify-core")

    assert s3_publisher.default_bucket() == "captify-core"


def test_load_service_aws_env_reads_configured_env_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWS_ACCESS_KEY_ID=from-file\n"
        "AWS_SECRET_ACCESS_KEY=from-file-secret\n"
        "AWS_REGION=us-test-1\n"
    )
    monkeypatch.setattr(
        docling_serve_settings,
        "deep_document_service_env_file",
        str(env_file),
    )
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    s3_publisher.load_service_aws_env()

    assert s3_publisher.resolve_service_env_file() == (env_file, True)
    assert s3_publisher.os.environ["AWS_ACCESS_KEY_ID"] == "from-file"
    assert s3_publisher.os.environ["AWS_SECRET_ACCESS_KEY"] == "from-file-secret"
    assert s3_publisher.os.environ["AWS_REGION"] == "us-test-1"


def test_configured_service_aws_env_overrides_process_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWS_ACCESS_KEY_ID=from-file\n")
    monkeypatch.setattr(
        docling_serve_settings,
        "deep_document_service_env_file",
        str(env_file),
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-process")

    s3_publisher.load_service_aws_env()

    assert s3_publisher.os.environ["AWS_ACCESS_KEY_ID"] == "from-file"


def test_implicit_service_aws_env_does_not_override_process_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWS_ACCESS_KEY_ID=from-file\n")
    monkeypatch.setattr(docling_serve_settings, "deep_document_service_env_file", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "from-process")

    s3_publisher.load_service_aws_env()

    assert s3_publisher.resolve_service_env_file() == (env_file, False)
    assert s3_publisher.os.environ["AWS_ACCESS_KEY_ID"] == "from-process"
