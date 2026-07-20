"""Narrow source-identity context shared by materialization and conversion."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class PublicSourceIdentity:
    original_name: str
    content_type: str
    original_uri: str | None


SourceIdentities = tuple[PublicSourceIdentity | None, ...]
_SOURCE_IDENTITIES: contextvars.ContextVar[SourceIdentities | None] = (
    contextvars.ContextVar("docling_source_identities", default=None)
)


def source_identities() -> SourceIdentities | None:
    return _SOURCE_IDENTITIES.get()


@contextmanager
def bind_source_identities(identities: SourceIdentities) -> Iterator[None]:
    token = _SOURCE_IDENTITIES.set(identities)
    try:
        yield
    finally:
        _SOURCE_IDENTITIES.reset(token)


__all__ = [
    "PublicSourceIdentity",
    "SourceIdentities",
    "bind_source_identities",
    "source_identities",
]
