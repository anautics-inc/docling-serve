"""Narrow public-source identity restorer for converter managers."""

from __future__ import annotations

from pathlib import PurePath
from typing import Protocol

from pydantic import AnyUrl, TypeAdapter

from docling.datamodel.document import ConversionResult

from docling_serve.ingestion.source_identity import source_identities


class SourceIdentityRestorer(Protocol):
    def __call__(self, result: ConversionResult, index: int) -> ConversionResult: ...


def restore_context_source_identity(
    result: ConversionResult, index: int
) -> ConversionResult:
    identities = source_identities()
    identity = (
        identities[index]
        if identities is not None and index < len(identities)
        else None
    )
    if identity is None:
        return result
    result.input.file = PurePath(identity.original_name)
    if result.document is not None and result.document.origin is not None:
        result.document.origin.filename = identity.original_name
        result.document.origin.mimetype = identity.content_type
        if identity.original_uri is not None:
            result.document.origin.uri = TypeAdapter(AnyUrl).validate_python(
                identity.original_uri
            )
    return result


__all__ = ["SourceIdentityRestorer", "restore_context_source_identity"]
