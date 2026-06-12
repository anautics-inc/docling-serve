"""Source connector contract.

A *connector* answers "where does the data come from" and yields uniform
:class:`IngestionItem` s — one per file/record to extract. The extractor layer
then answers "how do I turn this into the standard bundle". Keeping the two
separate means a new source (S3 prefix, Access database, an AWS service) only
needs a connector, and a new format only needs an extractor; they compose
through the ``IngestionItem`` + ``profile`` contract.

Connectors do not talk to Docling or write bundles; they only produce items
(bytes / local path / lazy loader) plus the ``suggested_profile`` that should
route each item to the right extractor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConnectorError(RuntimeError):
    """Raised when a connector cannot discover or load its source."""


@dataclass(slots=True)
class IngestionItem:
    """One source document to ingest.

    Exactly one of ``data``, ``local_path``, or ``loader`` provides the bytes.
    ``suggested_profile`` routes the item to an extractor (e.g. ``schematic``,
    ``access``); ``source_refs`` carries provenance (bucket/key, table, etc.).
    """

    name: str
    media_type: str | None = None
    suggested_profile: str = "default"
    source_refs: dict[str, Any] = field(default_factory=dict)
    data: bytes | None = None
    local_path: Path | None = None
    loader: Callable[[], bytes] | None = None

    def read_bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.local_path is not None:
            return Path(self.local_path).read_bytes()
        if self.loader is not None:
            return self.loader()
        raise ConnectorError(f"IngestionItem {self.name!r} has no data source.")


class Connector(ABC):
    """Base class for source connectors."""

    #: Identifier matched against the ``connector`` request field.
    name: str = "connector"

    @abstractmethod
    def discover(self, config: dict[str, Any]) -> Iterator[IngestionItem]:
        """Yield the items this connector exposes for ``config``."""
