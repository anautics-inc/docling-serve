"""Clean, typed document extraction service for docling-serve.

One entrypoint, dispatched by file type, producing one standard S3 bundle per
document regardless of source format:

    <prefix>/
      document.json     # structured / reassemblable (slides geometry, sections)
      document.md       # markdown reassembly (what the pipeline chunks)
      document.html     # html reassembly (viewable)
      media/<hash>.ext  # every extracted image
      extraction.json   # manifest: type, extractor, counts, files, media

Browser consumers read ``document.json`` + ``media/`` to reassemble/edit;
the document pipeline reads ``document.md`` to chunk for OpenSearch / NER / Neo4j.
"""

from __future__ import annotations

from docling_serve.extraction.service import assemble_document_bundle

__all__ = ["assemble_document_bundle"]
