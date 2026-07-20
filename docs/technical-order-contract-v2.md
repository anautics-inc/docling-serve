# Technical Order contract v2

`bom.json` uses `captify.bom.v2`. Its embedded full-document content uses
`captify.to.v2`.

## Compatibility

Version 2 is additive over `captify.bom.v1` and `captify.to-content.v1`.
All v1 keys, array locations, normalized values, and `*Raw` source
transcriptions remain present and retain their meanings. The new
`compatibleSchemas` arrays identify the corresponding v1 contract. Consumers
that dispatch on `schema` must accept the v2 identifiers; consumers that read
the v1 data fields require no projection or migration.

The source transcription fields are immutable evidence. Normalization,
classification, links, IDs, provenance, and markings are separate fields and
must never replace a raw value.

## Stable IDs

Pages, content blocks, figure sheets, hotspots, and parts-list entries have an
opaque `id`. IDs are deterministic SHA-256-derived values scoped to the source
PDF hash and a logical source locator. Re-extracting the same source with the
same parser produces the same IDs. IDs are not sequential database keys and
consumers must not parse their contents.

Relationships use IDs in addition to legacy locators:

- parts-list entries retain `parentSequence` and add `parentId`;
- figure-group sheets add `id`;
- hotspots add `figureSheetId`;
- figure-reference blocks add `figureSheetId`.

## Provenance and geometry

Extracted entities expose:

```json
{
  "provenance": {
    "method": "layout-text",
    "parser": {"name": "docling-serve.technical-order.parts-list", "version": "2"},
    "confidence": 1.0,
    "sourceGeometry": {
      "pageNumber": 8,
      "coordinateSystem": "normalized-page-top-left",
      "boundingBox": [0.1, 0.2, 0.9, 0.3]
    }
  }
}
```

`confidence` is in `[0, 1]`. `boundingBox` is emitted only when the extractor
has real coordinates; a page locator may be emitted without a box. Parts rows
use text-layer row boxes when available, while hotspots use detected glyph
boxes. Methods distinguish layout text, Tesseract OCR, and vision-model
extraction.

## Marking propagation

When the source title page supplies a distribution statement, it is preserved
verbatim as `document.distributionStatement` and represented in
`document.markings`. Pages, blocks, figure sheets, hotspots, and parts-list
entries inherit that marking with an `inheritedFrom` ID. No classification or
distribution marking is inferred when the source does not provide one.
