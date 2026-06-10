"""Connector registry and dispatch.

    resolve_connector("s3").discover(config) -> Iterator[IngestionItem]

``file`` is always available. Other built-ins can be gated by the
``DOCLING_SERVE_ALLOWED_CONNECTORS`` allow-list. Add a new source by
registering one :class:`Connector`.
"""

from __future__ import annotations

from docling_serve.connectors.accessdb_connector import AccessDbConnector
from docling_serve.connectors.aws_service_connector import (
    AwsServiceConnector,
    available_services,
    register_aws_handler,
)
from docling_serve.connectors.base import Connector, ConnectorError, IngestionItem
from docling_serve.connectors.file_connector import FileConnector
from docling_serve.connectors.s3_connector import S3Connector
from docling_serve.settings import docling_serve_settings

_CONNECTORS: dict[str, Connector] = {}


def _register(connector: Connector, *aliases: str) -> None:
    _CONNECTORS[connector.name] = connector
    for alias in aliases:
        _CONNECTORS[alias] = connector


_register(FileConnector())
_register(S3Connector())
_register(AccessDbConnector(), "access", "msaccess")
_register(AwsServiceConnector(), "aws", "aws_service")


def available_connectors() -> list[str]:
    """Connector names callers may request (honours the allow-list)."""
    allowed = getattr(docling_serve_settings, "allowed_connectors", None)
    names = sorted({c.name for c in _CONNECTORS.values()})
    if not allowed:
        return names
    permitted = {"file", *(a.strip().lower() for a in allowed)}
    return [name for name in names if name in permitted]


def resolve_connector(name: str) -> Connector:
    """Return the connector for ``name`` or raise :class:`ConnectorError`."""
    key = (name or "file").strip().lower()
    connector = _CONNECTORS.get(key)
    if connector is None:
        raise ConnectorError(
            f"Unknown connector {name!r}; available: {sorted(set(_CONNECTORS))}."
        )
    allowed = getattr(docling_serve_settings, "allowed_connectors", None)
    if allowed and connector.name != "file":
        permitted = {a.strip().lower() for a in allowed}
        if connector.name not in permitted and key not in permitted:
            raise ConnectorError(f"Connector {connector.name!r} is not allow-listed.")
    return connector


__all__ = [
    "AccessDbConnector",
    "AwsServiceConnector",
    "Connector",
    "ConnectorError",
    "FileConnector",
    "IngestionItem",
    "S3Connector",
    "available_connectors",
    "available_services",
    "register_aws_handler",
    "resolve_connector",
]
