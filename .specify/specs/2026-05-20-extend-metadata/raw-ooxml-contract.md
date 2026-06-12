# Raw OOXML Storage Contract

Date: 2026-05-21

## Decision

Production deep-mode outputs should keep two layers of PowerPoint style data:

1. A normalized API contract for common viewer/editor behavior.
2. Raw OOXML as a sidecar/debug artifact for audit and future fidelity work.

The primary API response should not inline every raw XML fragment by default.
It should expose normalized fields that the web viewer and tldraw/canvas
renderer can use without understanding OOXML internals. Raw OOXML should be
persisted alongside the manifest and referenced by stable source pointers.

## Rationale

The prototype proved that raw OOXML is valuable for verifying no style data is
lost: table styles, paragraph spacing, theme colors, placeholder inheritance,
and cell-level formatting can all require XML-level inspection. It also proved
that embedding all raw XML in the primary JSON makes artifacts and previews
large and hard to consume.

The production contract therefore separates operational data from forensic
data:

- Normalized style fields are the default for UI, agents, schemas, and APIs.
- Raw OOXML remains available for high-fidelity reconstruction, audits,
  regression debugging, and future extraction improvements.

## Primary Manifest

The primary manifest should retain normalized, file-neutral fields:

- element identity and source pointers
- geometry in native units plus display pixels
- text paragraphs/runs
- normalized font, color, paragraph spacing, indentation, and table styles
- asset references and extracted image/OCR context
- source part references such as `ppt/slides/slide14.xml`
- optional raw sidecar references, not full raw XML blobs

Example:

```json
{
  "elementId": "slide-014-block-002",
  "source": {
    "slidePart": "ppt/slides/slide14.xml",
    "shapeIndex": 2,
    "shapeName": "Content Placeholder 2"
  },
  "style": {
    "textBody": {},
    "shape": {},
    "rawRef": "raw-ooxml/ppt/slides/slide14.xml#shape-2"
  }
}
```

## Sidecar Artifact

Raw OOXML should be stored as one or more sidecar artifacts under the same
document export:

```text
raw-ooxml/
  ppt/presentation.xml
  ppt/theme/theme1.xml
  ppt/slideMasters/slideMaster1.xml
  ppt/slideLayouts/slideLayout1.xml
  ppt/slides/slide14.xml
  ppt/slides/_rels/slide14.xml.rels
  index.json
```

`index.json` should map normalized IDs to raw XML parts and fragment selectors:

```json
{
  "artifactKind": "captify.rawOoxmlIndex.v1",
  "elements": {
    "slide-014-block-002": {
      "part": "ppt/slides/slide14.xml",
      "selector": "shapeIndex:2",
      "relationshipsPart": "ppt/slides/_rels/slide14.xml.rels"
    }
  }
}
```

## API Rule

Default API responses should return normalized fields and sidecar references.
Debug/audit endpoints may return raw XML by explicit request and should be
size-limited.

## S3 Rule

When deep-mode artifacts are persisted, the ZIP/S3 output must include:

- normalized manifest JSON
- course-model artifacts
- generated preview artifacts, when requested
- asset files
- raw OOXML sidecar directory for PPTX inputs
- schema files

S3 persistence is not complete until an end-to-end target upload regression
proves these artifacts are present in the uploaded ZIP/object.
