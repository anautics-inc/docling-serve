"""Neutral artifact publication boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from docling_serve.storage import content_type_for


class ArtifactPublisher(Protocol):
    def publish_directory(
        self, local_dir: Path, *, bucket: str, prefix: str
    ) -> list[str]: ...


def publish_dir_to_s3(
    local_dir: Path,
    *,
    bucket: str,
    prefix: str,
    client_factory: Callable[[], Any] | None = None,
) -> list[str]:
    """Upload a directory while preserving relative paths and media types."""
    if client_factory is None:
        import boto3

        def client_factory() -> Any:
            return boto3.client("s3")

    client = client_factory()
    base = prefix.strip("/")
    keys: list[str] = []
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(local_dir).as_posix()
        key = f"{base}/{relative}" if base else relative
        client.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={"ContentType": content_type_for(path)},
        )
        keys.append(key)
    return keys


class S3ArtifactPublisher:
    def publish_directory(
        self, local_dir: Path, *, bucket: str, prefix: str
    ) -> list[str]:
        return publish_dir_to_s3(local_dir, bucket=bucket, prefix=prefix)
