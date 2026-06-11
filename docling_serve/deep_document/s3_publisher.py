from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docling_serve.settings import docling_serve_settings


@dataclass(frozen=True)
class PublishedObject:
    path: str
    bucket: str
    key: str
    content_type: str
    size_bytes: int


S3_REQUIRED_MESSAGE = (
    "Deep extraction needs an S3 bucket. Pass the 'deep_s3_bucket' form field "
    "on the request, or set DOCLING_SERVE_DEEP_DOCUMENT_S3_BUCKET on the server."
)


class DeepBucketNotAllowed(ValueError):
    """Raised when a request-supplied S3 bucket is not on the allow-list."""


def default_bucket() -> str:
    """Server-configured deep-document S3 bucket, or '' when unset."""
    return (
        docling_serve_settings.deep_document_s3_bucket.strip()
        or os.getenv("S3_BUCKET_NAME", "").strip()
    )


def deep_bucket_available(request_bucket: str = "") -> bool:
    """True when a deep-extraction S3 bucket can be resolved.

    A bucket may come from the request (``deep_s3_bucket`` form field) or from
    the server default. Either is enough.
    """
    return bool(request_bucket.strip() or default_bucket())


def allowed_buckets() -> set[str]:
    """Buckets a request may target. The server default is always included."""
    configured = {
        bucket.strip()
        for bucket in (docling_serve_settings.deep_document_s3_allowed_buckets or [])
        if bucket.strip()
    }
    default = default_bucket()
    if default:
        configured.add(default)
    return configured


def ensure_bucket_allowed(request_bucket: str) -> None:
    """Reject a request-supplied bucket that is not on the allow-list.

    An empty request bucket is fine: the server default is used instead. A
    non-empty request bucket must match the server default or an entry in
    ``deep_document_s3_allowed_buckets`` — otherwise a caller could direct the
    service to write its output to an arbitrary bucket with the service's own
    AWS credentials (confused-deputy / exfiltration).
    """
    bucket = request_bucket.strip()
    if not bucket:
        return
    if bucket not in allowed_buckets():
        raise DeepBucketNotAllowed(
            f"S3 bucket '{bucket}' is not allowed. Add it to "
            "DOCLING_SERVE_DEEP_DOCUMENT_S3_ALLOWED_BUCKETS to permit it."
        )


def resolve_deep_target(
    *,
    task_id: str,
    tenant_id: str | None = None,
    request_bucket: str = "",
    request_prefix: str = "",
) -> tuple[str, str]:
    """Resolve the (bucket, prefix) the deep object tree is published to.

    The request wins over the server default. Raises when no bucket is
    available from either source, or when a request bucket is not allow-listed.
    """
    ensure_bucket_allowed(request_bucket)
    bucket = request_bucket.strip() or default_bucket()
    if not bucket:
        raise RuntimeError(S3_REQUIRED_MESSAGE)
    if request_prefix.strip():
        prefix = normalize_prefix(request_prefix)
    else:
        prefix = configured_prefix(task_id=task_id, tenant_id=tenant_id)
    return bucket, prefix


def configured_prefix(*, task_id: str, tenant_id: str | None = None) -> str:
    template = docling_serve_settings.deep_document_s3_prefix_template
    rendered = template.format(
        task_id=safe_path_part(task_id),
        tenant_id=safe_path_part(tenant_id or "default"),
    )
    return normalize_prefix(rendered)


def upload_tree(
    *,
    root_dir: Path,
    bucket: str,
    prefix: str,
    client: Any | None = None,
) -> list[PublishedObject]:
    s3 = client or s3_client()
    published = []
    for path in sorted(item for item in root_dir.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root_dir).as_posix()
        key = f"{prefix.rstrip('/')}/{relative_path}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        published.append(
            PublishedObject(
                path=relative_path,
                bucket=bucket,
                key=key,
                content_type=content_type,
                size_bytes=path.stat().st_size,
            )
        )
    return published


def s3_client() -> Any:
    load_service_aws_env()

    import boto3  # type: ignore[import-not-found]

    region = docling_serve_settings.deep_document_s3_region or None
    if region:
        return boto3.client("s3", region_name=region)
    return boto3.client("s3")


def load_service_aws_env() -> None:
    """Load service AWS credentials before creating the S3 client.

    A configured service env file is authoritative for the deep-document
    uploader, so it may replace stale PM2 AWS variables. The implicit repo
    ``.env`` fallback remains non-invasive and only fills missing values.
    """
    env_file, override = resolve_service_env_file()
    if env_file is None:
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(env_file, override=override)


def resolve_service_env_file() -> tuple[Path | None, bool]:
    configured = docling_serve_settings.deep_document_service_env_file.strip()
    if configured:
        path = Path(configured).expanduser()
        return (path, True) if path.is_file() else (None, False)

    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for candidate in candidates:
        path = candidate.expanduser()
        if path.is_file():
            return path, False

    fallback = Path(os.getenv("DOCLING_SERVE_SERVICE_ENV_FILE", ""))
    if fallback and fallback.expanduser().is_file():
        return fallback.expanduser(), False
    return None, False


def normalize_prefix(value: str) -> str:
    text = value.strip().lstrip("/")
    # Drop empty/'.'/'..' segments defensively. This is an S3 key prefix, not a
    # filesystem path, but stripping traversal tokens keeps the key namespace
    # predictable and avoids surprising '..' literals in object keys.
    segments = [part for part in text.split("/") if part not in ("", ".", "..")]
    text = "/".join(segments)
    return text.rstrip("/") or "docling"


def safe_path_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._=-]+", "-", value).strip("-") or "task"
