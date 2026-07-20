# Captify ↔ Docling Serve native artifact contract

Generic conversion uses Docling's native pre-signed artifact storage. Captify
typed extractors publish their versioned form, BOM, and schematic bundles
separately; clients must select the contract by the reported capability/domain.

## Server configuration (env)

```env
DOCLING_SERVE_ARTIFACT_STORAGE_ENABLED=true
# Use the endpoint format required by the installed docling-jobkit release.
DOCLING_SERVE_ARTIFACT_STORAGE_ENDPOINT=s3.us-east-1.amazonaws.com
DOCLING_SERVE_ARTIFACT_STORAGE_BUCKET=captify-core
DOCLING_SERVE_ARTIFACT_STORAGE_KEY_PREFIX=tenants/
DOCLING_SERVE_ARTIFACT_STORAGE_PRESIGN_TTL_SECONDS=3600
# Prefer workload identity / IAM roles. Static keys are development-only.
# DOCLING_SERVE_ARTIFACT_STORAGE_ACCESS_KEY=...
# DOCLING_SERVE_ARTIFACT_STORAGE_SECRET_KEY=...
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

## Native artifact mapping

| Content | Native artifact |
|---|---|
| Lossless DoclingDocument | `artifact_type=json` |
| Markdown | `artifact_type=markdown` |
| HTML | `artifact_type=html` |
| Images and related resources | `artifact_type=resource_bundle` |
| Status and artifact list | the `documents[]` item |

## Consumer contract

- Generic clients read `documents[].artifacts[]` by `artifact_type`.
- Typed clients read the versioned bundle reported by `/v1/capabilities` and
  the extraction response's `domain`.
- Clients must not infer a typed contract from a filename or duplicate
  Docling Serve's content-routing heuristics.
