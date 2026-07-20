"""Executable ingestion adapter registry."""

from docling_serve.ingestion.adapters.registry import (
    ADAPTERS,
    adapter_readiness,
    execute_adapter,
    get_adapter,
    public_capabilities,
)

__all__ = [
    "ADAPTERS",
    "adapter_readiness",
    "execute_adapter",
    "get_adapter",
    "public_capabilities",
]
