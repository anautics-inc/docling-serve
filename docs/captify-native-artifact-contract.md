# Captify ↔ docling-serve 1.24 native artifact contract (Option A)

Proven end-to-end on **docling-serve 1.24.0** (docling-slim 2.102.2, docling-jobkit
1.23.1) in the `cap-af-jmj-docling124` worktree. This replaces the fork's custom
`deep_document` S3 bundle (`document.json`/`extraction.json`/`media/`) with
docling's **native pre-signed artifact storage**. Consumers (captify-pytology,
captify-core-workbench) adapt to the shape below.

## Server configuration (env)

```env
DOCLING_SERVE_ARTIFACT_STORAGE_ENABLED=true
# NOTE: scheme-less host — jobkit prepends "https://". "https://s3..." => "https://https:/..." (broken).
DOCLING_SERVE_ARTIFACT_STORAGE_ENDPOINT=s3.us-east-1.amazonaws.com
DOCLING_SERVE_ARTIFACT_STORAGE_BUCKET=captify-core
DOCLING_SERVE_ARTIFACT_STORAGE_KEY_PREFIX=tenants/
DOCLING_SERVE_ARTIFACT_STORAGE_PRESIGN_TTL_SECONDS=3600
# NOTE: jobkit's S3 client uses THESE keys, not the boto3 default chain / instance role.
DOCLING_SERVE_ARTIFACT_STORAGE_ACCESS_KEY=...
DOCLING_SERVE_ARTIFACT_STORAGE_SECRET_KEY=...
DOCLING_SERVE_ARTIFACT_STORAGE_VERIFY_SSL=true
```

Object keys are task-scoped: `{key_prefix}/{tenant}/{date}/{task_id}/{hash}/{filename}`,
with `tenant_id`/`user_id`/`project_id` written as S3 object metadata (from
`task.metadata`).

## Request

Async convert with the `presigned_url` target and the formats you want stored:

```
POST /v1/convert/file/async
  files=@<doc>
  target_type=presigned_url
  to_formats=md
  to_formats=json
  to_formats=html        # optional
  # images ride a resource_bundle artifact when generated
```

## Response (the contract)

`GET /v1/result/{task_id}` →

```jsonc
{
  "num_converted": 1, "num_succeeded": 1, "num_failed": 0, "processing_time": 0.2,
  "documents": [
    {
      "source_index": 0,
      "source_uri": "<filename>",
      "filename": "<filename>",
      "status": "success",
      "errors": [],
      "timings": {},
      "artifacts": [
        { "artifact_type": "json",     "mime_type": "application/json", "uri": "<presigned GET url>", "url_expires_at": "..." },
        { "artifact_type": "markdown", "mime_type": "text/markdown",    "uri": "<presigned GET url>", "url_expires_at": "..." }
        // also: html, text, doctags, resource_bundle (images) when requested/produced
      ]
    }
  ]
}
```

`artifact_type ∈ {json, html, markdown, text, doctags, resource_bundle}`.

## Mapping from the old captify bundle

| Old fork bundle | Native artifact |
|---|---|
| `document.json` | `artifact_type=json` (lossless DoclingDocument) |
| `document.md` | `artifact_type=markdown` |
| `document.html` | `artifact_type=html` |
| `media/*` | `artifact_type=resource_bundle` |
| `extraction.json` (manifest) | the `documents[]` item itself (status + artifact list) |

## Consumer adaptation (Option A)

- **captify-pytology** `DoclingServeClient`: replace `submit_deep_convert_job`
  (`extraction=deep` + custom bundle) with the native async convert above; read
  `documents[].artifacts[]` and hand the pre-signed URLs (or proxy them) downstream.
- **captify-core-workbench** notebook bundle reader: fetch artifacts by
  `artifact_type` from the pre-signed URIs instead of reading
  `document.json`/`extraction.json`/`media/` from a fixed prefix.
