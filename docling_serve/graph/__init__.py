"""Knowledge-graph extraction (NER replacement) over already-converted text.

Public surface for the ``/v1/graph/extract`` endpoint. Conversion itself is
docling's out-of-the-box job; this package only runs docling-graph on the text
docling produced.
"""

from docling_serve.graph.extraction import (
    GraphExtractionUnavailable,
    docling_graph_installed,
    graph_payload_from_text,
)

__all__ = [
    "GraphExtractionUnavailable",
    "docling_graph_installed",
    "graph_payload_from_text",
]
