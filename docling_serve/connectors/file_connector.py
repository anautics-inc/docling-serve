"""File connector: the default source for already-uploaded bytes.

Formalises the existing multipart-upload path as a connector so callers can
treat every source uniformly. Drawings and schematics arrive here too — they
are ordinary files routed to the schematic extractor by ``profile``, not a
separate connector.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from docling_serve.connectors.base import Connector, ConnectorError, IngestionItem


class FileConnector(Connector):
    name = "file"

    def discover(self, config: dict[str, Any]) -> Iterator[IngestionItem]:
        """Yield items from in-memory ``files`` and/or on-disk ``paths``.

        ``config`` keys:
          - ``files``: list of ``{"name", "data", "media_type"?}``
          - ``paths``: list of filesystem paths
          - ``profile``: profile applied to every yielded item
        """
        profile = str(config.get("profile") or "default")
        files = config.get("files") or []
        paths = config.get("paths") or []
        if not files and not paths:
            raise ConnectorError("file connector requires 'files' or 'paths'.")

        for entry in files:
            if not isinstance(entry, dict) or "data" not in entry:
                raise ConnectorError("file connector 'files' entries need 'data'.")
            yield IngestionItem(
                name=str(entry.get("name") or "upload"),
                media_type=entry.get("media_type"),
                suggested_profile=str(entry.get("profile") or profile),
                data=entry["data"],
                source_refs={"connector": self.name},
            )

        for raw_path in paths:
            path = Path(raw_path)
            if not path.is_file():
                raise ConnectorError(f"file connector path not found: {path}")
            yield IngestionItem(
                name=path.name,
                suggested_profile=profile,
                local_path=path,
                source_refs={"connector": self.name, "path": str(path)},
            )
