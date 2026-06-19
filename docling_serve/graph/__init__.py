"""docling-graph knowledge-graph extraction (the Comprehend NER replacement).

Exposes the stateless ``graph_payload_from_text`` entry point used by the
``POST /v1/graph/extract`` endpoint, plus the profile→template resolver.
"""

from docling_serve.graph.extraction import (
    GraphExtractionUnavailable,
    docling_graph_installed,
    graph_payload_from_text,
)
from docling_serve.graph.models import GraphExtractRequest, GraphExtractResponse
from docling_serve.graph.templates import PROFILE_TEMPLATES, resolve_profile_template

__all__ = [
    "PROFILE_TEMPLATES",
    "GraphExtractRequest",
    "GraphExtractResponse",
    "GraphExtractionUnavailable",
    "docling_graph_installed",
    "graph_payload_from_text",
    "resolve_profile_template",
]
