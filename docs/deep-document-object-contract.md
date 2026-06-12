# Deep Document Object Contract

**Audience:** Captify app engineers (Spaces upload flow, document viewer/editor).
**Purpose:** define the single object model docling-serve returns for deep extraction, so the app can render and edit it without knowing file-format internals.
**Status:** implemented in docling-serve. See §7 for behaviour notes.
**Last updated:** 2026-05-21.

---

## 1. Two extraction modes

The app picks the mode per upload with one `multipart/form-data` field named `extraction`, sent to `POST /v1/convert/file` or `POST /v1/convert/file/async`:

| `extraction` | When the app uses it | What docling returns |
|---|---|---|
| omitted or `default` | "Just use this file for context" — RAG, embedding, search. | Today's response, unchanged. In-body chunk/convert payload. Not covered by this doc. |
| `deep` | "I want to update or edit this file" — open it in the document viewer/editor. | The **deep document object** described here, published to S3 as an expanded object tree. |

`default` mode is untouched: same endpoints, same in-body response, same Spaces ingestion path. Everything below applies to `extraction=deep` only.

Deep extraction is **pure structural extraction** — units, elements, geometry, images, canvas. It contains no course-model, module, slide-role, Bloom, or pedagogical fields. Anything instructional is an app-side concern layered on top of this object.

---

## 2. One object model, every file type

`deep` extraction always returns the **same object shape** regardless of source file type. A PowerPoint, PDF, Word doc, and Excel workbook all come back as `deep-document.json` with `document.units[]`.

What changes between file types is **fill level**, not structure:

| Field | PPTX | PDF | DOCX | XLSX |
|---|---|---|---|---|
| `unit` = | slide | page | section | sheet |
| `unit.render.size` (EMU/inch/px) | full | px only | absent | absent |
| `element.bbox` XY coordinates | EMU + inch + px | px | absent/partial | cell grid |
| `element.sourceRefs` source pointers | full (`ppt/slides/slideN.xml`) | page/item ref | item ref | sheet/cell ref |
| `unit.render.background` | full | partial | absent | absent |
| `content.speakerNotes` | populated | empty | empty | empty |

The app reads one schema. Missing data is an **empty object/array/null**, never a different shape. Branch on `unitType` only when you need format-specific affordances (e.g. redraw geometry exists for PPTX/PDF, not DOCX).

---

## 3. S3 handoff

`deep` mode **always** publishes an expanded object tree to S3 — one S3 object per file. There is no ZIP: the app never downloads or unpacks an archive.

### 3.1 The app supplies the S3 destination per request

The app does **not** need any server-side S3 env vars. It passes the destination as `multipart/form-data` fields on the deep request:

| Form field | Meaning |
|---|---|
| `deep_s3_bucket` | S3 bucket to publish the deep object tree to — normally the same bucket the app already stored the source upload in. |
| `deep_s3_prefix` | Key prefix under that bucket, e.g. `documents/{documentId}/docling`. The app owns this layout. |

So a deep submit looks like:

```
POST /v1/convert/file/async
  files=@source.pptx
  extraction=deep
  deep_s3_bucket=acme-documents
  deep_s3_prefix=documents/doc-42/docling
```

docling publishes the tree under `s3://acme-documents/documents/doc-42/docling/`. Because the app chose the bucket and prefix, it already knows exactly where every artifact lands — no prefix guessing, no read-URL lookup needed.

A server default may optionally be configured (`DOCLING_SERVE_DEEP_DOCUMENT_S3_BUCKET`, `..._S3_PREFIX_TEMPLATE`, `..._S3_REGION`); the request fields override it when present. `DOCLING_SERVE_DEEP_DOCUMENT_S3_REGION` is the one setting still read from the server (AWS region/credentials).

If **neither** the request nor a server default supplies a bucket, the submit fails fast with **HTTP 503** and a message naming `deep_s3_bucket`. There is no ZIP fallback and no in-body fallback.

### 3.2 Object key layout

With `deep_s3_bucket=acme-documents` and `deep_s3_prefix=documents/doc-42/docling`:

```text
documents/doc-42/{originalFileName}                ← app-owned, the original upload (app stores this)
documents/doc-42/docling/                          ← docling-owned, generated tree
  deep-document-package.json                       ← ROOT ENTRYPOINT
  {stem}.json  {stem}.html  {stem}.md              ← standard docling exports
  artifacts/*.png                                  ← referenced images
  {stem}_deep_document/
    deep-document.json                             ← PRIMARY OBJECT
    schemas/
      deep-document.schema.json
```

If `deep_s3_prefix` is omitted, docling falls back to the server prefix template (`documents/{tenant_id}/docling/{task_id}`). Passing `deep_s3_prefix` explicitly is recommended so the app controls the layout.

### 3.3 Read order

1. Read **`deep-document-package.json`** — the root manifest. Never hardcode any other filename.
2. From it, resolve `entrypoints.deepDocuments[0]` and load **`deep-document.json`** — the primary object.
3. Load images and other artifacts lazily from the `files[]` list.

The async result (`GET /v1/result/{task_id}`) carries no document body for deep mode — the output lives in S3. The app locates the package from the known prefix (`tenant_id` + `task_id` + template) or from the prefix it persisted at submit time.

### 3.4 `deep-document-package.json`

```jsonc
{
  "schemaVersion": "1.0",
  "artifactKind": "deep_document_package",
  "taskId": "<docling task id>",
  "createdAt": "<ISO-8601 UTC>",
  "storage": { "bucket": "...", "prefix": "documents/.../docling/{taskId}", "layout": "expanded_object_tree" },
  "entrypoints": {
    "deepDocuments": ["{stem}_deep_document/deep-document.json"]
  },
  "files": [
    {
      "path": "{stem}_deep_document/deep-document.json",
      "kind": "json",
      "contentType": "application/json",
      "sizeBytes": 48213,
      "s3": { "bucket": "...", "key": "documents/.../deep-document.json" }
    }
    // ... one record per generated file
  ]
}
```

`path` is always relative to the package root. `files[].s3` carries the absolute object key.

---

## 4. `deep-document.json` — the primary object

This is the object the app's document viewer/editor consumes.

```jsonc
{
  "schemaVersion": "1.0",
  "artifactKind": "deep_document",
  "documentId": "<derived from source, or carried through when supplied>",
  "sourceManifestKey": "<stable pointer back to the extraction manifest>",
  "createdAt": "<ISO-8601 UTC>",

  "source": {
    "originalFileName": "afto.pptx",
    "sha256": "...",
    "sourceManifestKey": "task:<taskId>:afto"
  },

  "storage": {
    "layout": "relative_object_tree",
    "manifestPath": "deep-document.json"
  },

  "document": {
    "title": "AFTO Form 874",
    "unitType": "slide",          // slide | page | section | sheet | mixed
    "unitCount": 27,
    "units": [ /* Unit[] — see §4.1 */ ]
  },

  "assets":       [ /* Asset[] — referenced images, see §4.4 */ ],
  "canvas":       { /* CanvasContract — tldraw redraw map, see §4.5 */ },
  "rawArtifacts": { "xmlParts": {}, "theme": {} },
  "provenance":   { "generator": "docling_serve.deep_document.document_builder" },
  "errors":       [ { "stage": "...", "message": "...", "recoverable": true } ]
}
```

There is no `courseModel`, `analysisSummary`, `reengineeringInput`, `pedagogical`, or `providerUsage` — docling runs no inference and produces no instructional analysis.

### 4.1 Unit

A `unit` is one slide / page / section / sheet. Same shape for every file type.

```jsonc
{
  "unitId": "unit-0001",          // stable; all cross-references use this
  "unitType": "slide",
  "unitNumber": 1,                // 1-indexed
  "title": "AFTO Form 874",

  "sourceRefs": {
    "sourcePart": "ppt/slides/slide1.xml",   // PPTX: source part; other formats: page/section ref
    "sourceManifestKey": "task:<taskId>:afto"
  },

  "render": {                     // how to redraw the unit
    "size":       { "emu": {"w":9144000,"h":6858000}, "inches": {"w":10.0,"h":7.5}, "px": {"w":960,"h":720} },
    "background": { "kind": "solid", "color": "#FFFFFF", "source": "master", "assetId": null }
  },

  "content": {
    "header":    "AFTO Form 874",
    "subHeader": "Part E - Minor Assemblies",
    "plainText": "...",                       // concatenated text, convenience field
    "speakerNotes": { "raw": "", "cleaned": "" },
    "tables":   [ /* elements where type == "table" */ ],
    "images":   [ /* elements where type == "image" */ ],
    "elements": [ /* Element[] — full ordered element list, see §4.2 */ ]
  },

  "canvas": {                     // per-unit canvas wiring
    "frameId": "frame-unit-0001",
    "shapeIds": ["shape-unit-0001-element-0001", "..."]
  }
}
```

### 4.2 Element — the redraw unit

Every visible thing on a unit is an `element`. Elements carry the **XY coordinates** needed to redraw the document.

```jsonc
{
  "elementId": "slide-001-block-002",   // stable
  "type": "text",                       // text | image | table | unknown
  "kind": "body_placeholder",           // finer-grained source kind
  "zIndex": 4,

  "bbox": {                             // position + size; the XY coordinates for redraw
    "emu":    { "x": 0, "y": 992893, "w": 9144000, "h": 45720 },
    "inches": { "x": 0.0, "y": 1.0858, "w": 10.0, "h": 0.05 },
    "px":     { "x": 0.0, "y": 104.24, "w": 960.0, "h": 4.8 }
  },

  "text":  { "plain": "...", "paragraphs": [], "runs": [] },   // runs carry font/color/underline
  "style": { /* normalized font, color, paragraph spacing, indentation, table style */ },

  "assetRef": "asset-1b0bc55d",         // null unless type == "image"
  "sourceRefs": {
    "slidePart": "ppt/slides/slide1.xml",
    "shapeIndex": 2,
    "shapeName": "Content Placeholder 2",
    "placeholderType": "body",
    "inheritedFrom": "ppt/slideMasters/slideMaster1.xml"
  },
  "quality": { "reviewRequired": false, "warnings": [] }
}
```

Coordinate rules:
- **`px` is the canonical render space** — origin top-left, matches `unit.render.size.px`. Use `px` for the viewer and tldraw.
- `emu` / `inches` are present for PPTX (lossless geometry). For PDF, expect `px` only. For DOCX, `bbox` may be empty — flow layout has no fixed coordinates.
- `zIndex` orders overlapping shapes back-to-front.

### 4.3 Image list inside the objects

When `extraction=deep`, every extracted image is (a) written as its own S3 object under `artifacts/`, (b) listed in `deep-document.json.assets[]`, and (c) referenced from the element that uses it via `element.assetRef`.

To render a unit's images: take `unit.content.images[]` (elements), follow each `assetRef` into `assets[]`, use `asset.s3.key` or `asset.path`. The app never parses markdown or base64.

### 4.4 Asset

```jsonc
{
  "assetId": "asset-1b0bc55d",
  "kind": "exported_image",
  "role": "display",                    // display | master | header
  "path": "artifacts/image-001.png",    // relative to the package root
  "contentType": "image/png",
  "sizeBytes": 18422,
  "display": true,
  "s3": { "bucket": "...", "key": "documents/.../artifacts/image-001.png" }
}
```

Master/header images carry `role: "master"` / `"header"` and are for visual fidelity only.

### 4.5 Canvas contract

`deep-document.json.canvas` is a tldraw-oriented redraw map. The editor uses it to wire each unit to canvas frames/shapes.

```jsonc
{
  "provider": "tldraw",
  "coordinateSystem": { "unit": "px", "origin": "top-left" },
  "documentFrame": { "layout": "vertical", "unitSpacing": 120 },
  "shapeMap": {
    "unit-0001": {
      "frameId": "frame-unit-0001",
      "sourceImageShapeId": "shape-unit-0001-original",
      "editableGroupId": "group-unit-0001-editable",
      "notesShapeId": "shape-unit-0001-notes",
      "jsonShapeId": "shape-unit-0001-json"
    }
  }
}
```

The published `deep-document.schema.json` (shipped under `{stem}_deep_document/schemas/`) is the machine-checkable version of this contract.

---

## 5. What the app owns

docling stops at structure. Course modelling, modules, slide roles, learning objectives, Bloom classification, sequencing, assessment detection, and reengineering are **entirely outside deep extraction**. The app decides — for example by asking the user "is this a course?" — and runs that work itself against `deep-document.json`. docling does not produce, reserve space for, or care about any of it.

---

## 6. What the app should persist on its document record

- original source S3 bucket/key
- docling `taskId`
- `deep-document-package.json` S3 bucket/key
- generated output prefix
- primary `deep-document.json` S3 key
- the selected `extraction` mode (`default` | `deep`)
- processing status and error details

With the package key on the record, the UI loads everything else from `files[]` without hardcoding filenames beyond the root entrypoint.

---

## 7. Behaviour notes

- `extraction=deep` with no `deep_s3_bucket` form field and no server default bucket → **HTTP 503** at submit. No partial result is produced.
- `extraction=deep` succeeds → output is published to the S3 expanded tree at the app-supplied `deep_s3_bucket`/`deep_s3_prefix`; the convert result carries no document body (the app reads S3 at the location it chose).
- `extraction=default` or omitted → unchanged legacy behaviour; not affected by any deep-extraction setting.
- Course extraction / Bloom is a separate capability tracked under `.specify/specs/2026-05-20-extend-metadata/`; it is not part of deep extraction and not part of this contract.
