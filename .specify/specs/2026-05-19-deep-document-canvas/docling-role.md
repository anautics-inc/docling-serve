# Decision Record: Docling's Role in Document Extraction

**Date:** 2026-05-20
**Status:** Accepted
**Applies to:** the deep-document extraction pipeline (experiment6 onward)

## Context

Experiments 2–5 drifted. Docling was the extractor in experiment1, but each
time Docling's PPTX output was found missing something (speaker notes,
typography, theme, master inheritance) the gap was patched by parsing OOXML
directly. Gap by gap, `ooxml.py` became the entire extraction engine and
Docling decayed into an unused provenance sidecar.

That is a dead end. An OOXML-centric pipeline **cannot extract a PDF** — there
is no OOXML in a PDF. The product must ingest PPT, Word, PDF, and Excel
through one pipeline.

## Decision

**Docling's `DoclingDocument` JSON is the structural spine for every format.**
It produces `units`, `blocks`, `tables` (with cells), `pictures`, reading
order, and bounding boxes. The same code consumes it whether the source was
PPTX, PDF, DOCX, or XLSX.

**OOXML parsing is a PPTX-only enrichment layer.** It attaches exactly the
four things Docling's PPTX backend documents that it does not extract:

- speaker notes
- typography (run-level font/size/colour, inheritance-resolved)
- theme (colour + font scheme)
- slide background

For a non-OOXML source (PDF), the spine stands alone and enrichment is
skipped — no rewrite, no parallel engine.

## Consequences

- `docling_document.py` owns structure. `ooxml_enrichment.py` owns the four
  enrichment fields. Neither re-implements the other.
- Table cells come from Docling's `TableData` — the gap experiments 2–5
  deferred is closed for free.
- For PPTX, running Docling costs ~6–20 s/deck to produce structure that
  OOXML could also yield. That cost is accepted: it buys one extraction
  contract that also works for PDF/DOCX/XLSX.
- The join between Docling text blocks and OOXML typography is by normalized
  text content (Docling PPTX bboxes are shape-level, so a geometric join is
  not reliable). Current corpus match rate: ~92 %.

## Guardrail — do not let this drift again

Before adding any OOXML parsing, ask: *is this one of the four enrichment
fields above?* If not, it belongs in the Docling spine or in a Docling
post-processor — not in a parallel OOXML extractor. A reviewer seeing new
structural extraction in `ooxml_enrichment.py` should reject it.

## Where Docling genuinely earns its keep

- **PDF**: layout analysis + OCR — irreplaceable; there is no OOXML.
- **DOCX**: OMML→LaTeX equations, comment extraction.
- **All formats**: one `DoclingDocument` shape → one downstream contract.
- **Picture description**: the `do_picture_description` VLM enrichment is the
  Docling feature that captions opaque images — wired in experiment6 as the
  `image_captioner` Bedrock-vision path.
