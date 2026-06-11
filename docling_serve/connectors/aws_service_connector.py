"""Generic AWS-service connector.

Ingests data that originates from an AWS service rather than a raw file. The
service is selected by ``config["service"]`` and dispatched to a registered
handler, so new services are added by registering a handler — not by editing
this class. Ships with:

  - ``s3``: delegates to :class:`S3Connector` (objects under a prefix).
  - ``textract``: runs Amazon Textract on a single S3 object and yields the
    detected text as one ingestion item.

Register additional handlers (HealthLake, Comprehend Medical, etc.) with
:func:`register_aws_handler`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from docling_serve.connectors.base import Connector, ConnectorError, IngestionItem
from docling_serve.connectors.s3_connector import S3Connector

AwsServiceHandler = Callable[[dict[str, Any]], Iterator[IngestionItem]]

_HANDLERS: dict[str, AwsServiceHandler] = {}


def register_aws_handler(service: str, handler: AwsServiceHandler) -> None:
    _HANDLERS[service.strip().lower()] = handler


def available_services() -> list[str]:
    return sorted(_HANDLERS)


class AwsServiceConnector(Connector):
    name = "aws-service"
    aliases = ("aws", "aws_service")

    def discover(self, config: dict[str, Any]) -> Iterator[IngestionItem]:
        service = str(config.get("service") or "").strip().lower()
        if not service:
            raise ConnectorError(
                "aws-service connector requires 'service' "
                f"(one of {available_services()})."
            )
        handler = _HANDLERS.get(service)
        if handler is None:
            raise ConnectorError(
                f"Unknown AWS service {service!r}; registered: {available_services()}."
            )
        yield from handler(config)


def _s3_handler(config: dict[str, Any]) -> Iterator[IngestionItem]:
    yield from S3Connector().discover(config)


def _textract_handler(config: dict[str, Any]) -> Iterator[IngestionItem]:
    """Run synchronous Textract text detection on one S3 object."""
    bucket = config.get("bucket")
    key = config.get("key")
    if not (bucket and key):
        raise ConnectorError("textract handler requires 'bucket' and 'key'.")
    region = config.get("region")
    try:
        import boto3
    except ImportError as err:  # pragma: no cover
        raise ConnectorError("boto3 is not installed; cannot reach Textract.") from err
    client = boto3.client("textract", region_name=region)
    try:
        response = client.detect_document_text(
            Document={"S3Object": {"Bucket": bucket, "Name": key}}
        )
    except Exception as err:
        raise ConnectorError(f"Textract detect_document_text failed: {err}") from err

    lines = [
        block.get("Text", "")
        for block in response.get("Blocks", [])
        if block.get("BlockType") == "LINE"
    ]
    text = "\n".join(line for line in lines if line)
    name = str(key).rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    yield IngestionItem(
        name=f"{stem}.txt",
        media_type="text/plain",
        suggested_profile=str(config.get("profile") or "default"),
        data=text.encode("utf-8"),
        source_refs={
            "connector": "aws-service",
            "service": "textract",
            "bucket": bucket,
            "key": key,
        },
    )


register_aws_handler("s3", _s3_handler)
register_aws_handler("textract", _textract_handler)
