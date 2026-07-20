"""Request/response models for ``POST /v1/graph/extract``."""

from __future__ import annotations

from pydantic import BaseModel, Field


class GraphExtractRequest(BaseModel):
    """Request body for ``POST /v1/graph/extract``."""

    text: str = Field(
        description="Converted document text/markdown to extract a graph from"
    )
    template: str | None = Field(
        default=None,
        description="Dotted import path to a docling-graph Pydantic template; "
        "empty uses the configured default (generic entity/relation graph).",
    )
    profile: str | None = Field(
        default=None,
        description="Extraction profile (e.g. schematic, access, usaf-sustainment); selects the "
        "matching domain template when no explicit template is given.",
    )


class GraphExtractResponse(BaseModel):
    """Typed entity + relationship graph extracted from text.

    The ``{nodes, edges, labels, edgeLabels}`` shape is the contract the captify
    ingestion pipeline (``captify_enterprise.search.graph_entities``) consumes. An
    empty graph carrying a ``note`` is returned when extraction is unavailable, so
    callers degrade uniformly instead of handling an error.
    """

    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    labels: dict[str, int] = Field(default_factory=dict)
    edgeLabels: dict[str, int] = Field(default_factory=dict)
    nodeCount: int = 0
    edgeCount: int = 0
    template: str | None = None
    model: dict | None = None
    extractedModels: int | None = None
    note: str | None = None
