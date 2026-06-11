# Captify S3 to Docling Upload Flow

**Audience:** Captify and docling-serve engineers.
**Purpose:** explain what happens after Captify uploads a source document, what Docling ingests, and which output files become available for Captify to render or process.
**Last updated:** 2026-05-21.

## Current State

Captify sends uploaded files to docling-serve as multipart form data. Generic
deep extraction is requested with `extraction=deep` and publishes Docling
outputs as an expanded S3 object tree. (The former PowerPoint courseware
prototype layer was removed 2026-06-11; PPTX goes through the generic
python-pptx deep-extraction path.)

The deep extraction contract uses:

- `POST /v1/convert/file/async`
- `extraction=deep`
- `deep_s3_bucket=<app-owned bucket>`
- `deep_s3_prefix=<app-owned prefix>`
- output formats forced by the server: `json`, `html`, `md`, `text`
- `image_export_mode=referenced`
- source file: `afto.pptx`

Deep mode returns no ZIP/document body. Captify reads
`{deep_s3_prefix}/deep-document-package.json` from S3 first.

## End-to-End Flow

The intended production flow is:

1. Captify accepts a user upload.
2. Captify stores the original source file in S3.
3. Captify submits the file to docling-serve for conversion.
4. Docling converts the source document.
5. The deep-document wrapper builds normalized structural artifacts from the
   Docling conversion result.
6. Docling writes a package manifest that lists every generated file.
7. Docling uploads every generated output file as a separate S3 object.
8. Captify reads `deep-document-package.json` first, then loads the listed
   `deep-document.json`, images, schemas, and standard exports.

S3 should be treated as an object store, not a place where ZIP files are automatically unpacked. The production-friendly shape is the expanded object tree: one S3 object per output file, with a manifest that tells Captify what exists and where it is.

## Source File

The original uploaded file should remain available as its own S3 object owned by Captify. Docling's generated artifacts should reference the original upload by Captify document ID, task ID, source filename, and source S3 key once that handoff is wired.

Recommended source key shape:

```text
documents/{tenantId}/{documentId}/source/{originalFileName}
```

Recommended generated-output key shape:

```text
documents/{tenantId}/{documentId}/docling/{taskId}/...
```

The app should pass the target bucket/prefix per request:

```text
deep_s3_bucket=<bucket>
deep_s3_prefix=documents/{tenantId}/{documentId}/docling
```

A server default bucket/prefix template exists only as fallback. The request
fields should be preferred so Captify owns the object layout.

## Files Produced

For a single uploaded file named `afto.pptx`, the expanded S3 tree contains this
generic shape:

```text
deep-document-package.json
afto.json
afto.html
afto.md
artifacts/*.png
afto_deep_document/
  deep-document.json
  schemas/
    deep-document.schema.json
```

The exact `artifacts/*.png` count depends on the input file and selected image export mode.

## Entrypoint Files

Captify should start with:

```text
deep-document-package.json
```

This root manifest has:

- `artifactKind: "deep_document_package"`
- `taskId`
- `storage.bucket`
- `storage.prefix`
- `entrypoints.deepDocuments`
- `entrypoints.courseModels`
- `files[]`

Each `files[]` record includes:

- relative `path`
- `kind`
- `contentType`
- `sizeBytes`
- `s3.bucket` and `s3.key` when S3 publishing is enabled

After reading the package manifest, Captify should load:

```text
{source_stem}_course_artifacts/deep-document.json
```

That is the primary UI contract for the digital document viewer/editor.

## Primary UI Object

`deep-document.json` is the object Captify should use for the online document experience. It includes:

- document title and unit count
- normalized units, usually slides/pages/sheets depending on file type
- unit-level header, subheader, body text, speaker notes, tables, images, and elements
- render metadata such as page/slide size and background when available
- element bounding boxes and style fields when extraction provides them
- pedagogical metadata, including Bloom-oriented training fields where available
- course model summary
- reengineering input
- canvas contract for tldraw-oriented rendering
- asset references for generated images
- provider usage and errors

Important fields for Captify:

```json
{
  "artifactKind": "deep_document",
  "document": {
    "units": [
      {
        "unitId": "unit-0001",
        "unitType": "slide",
        "unitNumber": 1,
        "title": "AFTO Form 874",
        "render": {
          "size": {},
          "background": {}
        },
        "content": {
          "header": "AFTO Form 874",
          "subHeader": "Part E - Minor Assemblies...",
          "plainText": "...",
          "speakerNotes": {},
          "tables": [],
          "images": [],
          "elements": []
        },
        "pedagogical": {},
        "canvas": {}
      }
    ]
  },
  "assets": [],
  "courseModel": {},
  "analysisSummary": {},
  "reengineeringInput": {},
  "canvas": {}
}
```

## Supporting Artifacts

`course-model.json`

Captures the normalized training/course structure. This is the best artifact for instructional design workflows, module breakdowns, Bloom taxonomy review, course objectives, slide roles, and reengineering candidates.

`course-analysis-summary.json`

Provides a smaller summary of the course model, including module-level findings and high-priority issues.

`reengineering-input.json`

Packages the content in a shape intended for later agent workflows, redesign, remediation, and publishing.

`enriched-manifest.json`

Keeps a normalized reference back to the extracted document structure with course-model enrichments attached.

`afto.json`

The standard Docling JSON export. This remains useful for debugging and for any consumer that needs the raw Docling output.

`afto.html`

The standard Docling HTML export. This is useful as a quick human-readable reference, not the primary editable UI contract.

`afto.md`

The standard Markdown export. This remains useful for search, summaries, and legacy Captify flows that only consume Markdown.

`artifacts/*.png`

Referenced image files exported by Docling. When S3 publishing is enabled, each image is uploaded as its own object and listed in `deep-document-package.json`. `deep-document.json.assets[]` also receives display asset references for these exported images.

## S3 Publishing Behavior

The S3 publisher uploads the entire output directory file-by-file.

Current code path:

- `docling_serve/deep_document/export_results.py`
- `docling_serve/deep_document/document_builder.py`
- `docling_serve/deep_document/s3_publisher.py`

Request fields:

```text
extraction=deep
deep_s3_bucket=<bucket>
deep_s3_prefix=<prefix>
```

When enabled, the package manifest predicts and records S3 locations like:

```json
{
  "path": "afto_course_artifacts/deep-document.json",
  "s3": {
    "bucket": "captify-documents",
    "key": "docling/task-001/afto_course_artifacts/deep-document.json"
  }
}
```

The publisher sets `ContentType` from the file extension for each object.

## ZIP Versus Expanded S3 Tree

For API testing and simple response handling, `target_type=zip` is useful. It returns all generated files in one response archive.

For production Captify storage, prefer the expanded S3 tree. Captify should not have to download a ZIP, unpack it, and then re-upload individual files. S3 does not unzip objects automatically. If a ZIP is stored, something else must download and extract it.

Recommended production behavior:

1. Store source file in S3.
2. Run Docling.
3. Publish generated Docling files individually to S3.
4. Store the generated package prefix and `deep-document-package.json` key on the Captify document record.
5. Let the UI load JSON and image assets directly from their S3-backed URLs.

## Current Gaps Before Production Sign-Off

- Live S3 target upload has not been fully exercised end to end in this environment.
- The package prefix currently only templates `{task_id}`; Captify document/tenant IDs should be added if those are required in object keys.
- The source file's Captify S3 key is not yet stamped through every generated artifact.
- In-body single-document responses are enriched, but file-tree package artifacts are only produced for ZIP/remote-style output paths.
- Chunk endpoints still follow the existing Docling chunk response contract and do not yet emit the full deep-document package.

## What Captify Should Persist

Captify should persist at least:

- original source S3 bucket/key
- docling task ID
- generated package S3 bucket/key for `deep-document-package.json`
- generated output prefix
- primary `deep-document.json` key
- primary `course-model.json` key
- processing status and error details
- selected extraction mode/profile
- provider usage/cost metadata when Bedrock is enabled

The UI can then render from the package manifest without hardcoding file names beyond the root entrypoint.
