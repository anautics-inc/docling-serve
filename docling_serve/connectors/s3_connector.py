"""S3 connector: ingest every object under a bucket/prefix.

Lists objects with boto3 and yields one lazily-loaded :class:`IngestionItem`
per object so large prefixes don't all load into memory at once. Credentials
use the standard boto3 chain. The per-object byte ceiling comes from
``DOCLING_SERVE_CONNECTOR_MAX_OBJECT_BYTES``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from docling_serve.connectors.base import Connector, ConnectorError, IngestionItem
from docling_serve.settings import docling_serve_settings


class S3Connector(Connector):
    name = "s3"

    def discover(self, config: dict[str, Any]) -> Iterator[IngestionItem]:
        """Yield items for objects under ``bucket``/``prefix``.

        ``config`` keys: ``bucket`` (required), ``prefix``, ``region``,
        ``profile``, ``suffixes`` (optional allow-list like ``[".pdf", ".accdb"]``).
        """
        bucket = config.get("bucket")
        if not bucket:
            raise ConnectorError("s3 connector requires 'bucket'.")
        prefix = str(config.get("prefix") or "")
        profile = str(config.get("profile") or "default")
        region = config.get("region") or _default_region()
        suffixes = config.get("suffixes")
        max_bytes = int(
            getattr(docling_serve_settings, "connector_max_object_bytes", 512 * 1024 * 1024)
        )

        client = _s3_client(region)
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key")
                size = int(obj.get("Size") or 0)
                if not key or key.endswith("/") or size == 0:
                    continue
                if suffixes and not any(key.lower().endswith(s.lower()) for s in suffixes):
                    continue
                if size > max_bytes:
                    raise ConnectorError(
                        f"s3://{bucket}/{key} is {size} bytes, over the "
                        f"{max_bytes} byte connector limit."
                    )
                yield IngestionItem(
                    name=key.rsplit("/", 1)[-1],
                    suggested_profile=profile,
                    loader=_object_loader(client, bucket, key),
                    source_refs={"connector": self.name, "bucket": bucket, "key": key},
                )


def _object_loader(client: Any, bucket: str, key: str):
    def _load() -> bytes:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    return _load


def _s3_client(region: str | None) -> Any:
    try:
        import boto3
    except ImportError as err:  # pragma: no cover - boto3 is a hard dep
        raise ConnectorError("boto3 is not installed; cannot reach S3.") from err
    return boto3.client("s3", region_name=region)


def _default_region() -> str | None:
    import os

    return (
        getattr(docling_serve_settings, "deep_document_s3_region", None)
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
