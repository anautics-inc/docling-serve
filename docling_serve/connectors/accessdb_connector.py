"""Access database connector.

Yields the ``.mdb``/``.accdb`` file as a single ingestion item routed to the
``access`` profile. The connector locates the database (local path or, when a
bucket/key is given, S3); the :class:`AccessExtractor` does the table-level
expansion via mdbtools. This keeps "where" (connector) and "how" (extractor)
cleanly separated.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from docling_serve.connectors.base import Connector, ConnectorError, IngestionItem


class AccessDbConnector(Connector):
    name = "accessdb"
    aliases = ("access", "msaccess")

    def discover(self, config: dict[str, Any]) -> Iterator[IngestionItem]:
        """Yield one item for the Access database referenced by ``config``.

        ``config`` keys: ``path`` (local file) OR ``bucket`` + ``key`` (S3),
        plus optional ``region``.
        """
        path = config.get("path")
        if path:
            db_path = Path(path)
            if not db_path.is_file():
                raise ConnectorError(f"Access database not found: {db_path}")
            yield IngestionItem(
                name=db_path.name,
                media_type="application/x-msaccess",
                suggested_profile="access",
                local_path=db_path,
                source_refs={"connector": self.name, "path": str(db_path)},
            )
            return

        bucket = config.get("bucket")
        key = config.get("key")
        if not (bucket and key):
            raise ConnectorError("accessdb connector requires 'path' or 'bucket'+'key'.")
        yield IngestionItem(
            name=str(key).rsplit("/", 1)[-1],
            media_type="application/x-msaccess",
            suggested_profile="access",
            loader=_s3_loader(bucket, key, config.get("region")),
            source_refs={"connector": self.name, "bucket": bucket, "key": key},
        )


def _s3_loader(bucket: str, key: str, region: str | None):
    def _load() -> bytes:
        try:
            import boto3
        except ImportError as err:  # pragma: no cover
            raise ConnectorError("boto3 is not installed; cannot reach S3.") from err
        client = boto3.client("s3", region_name=region)
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()

    return _load
