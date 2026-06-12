# Captify security model & per-user spend attribution

This fork runs as a **private internal service** behind the captify gateway
(captify-pytology). This document is the contract for how requests are
authenticated, how the calling user's identity flows through the extraction
pipeline, and how every model call is attributed to that user in LiteLLM
spend logs.

## Topology and trust boundary

```
Browser / Workbench UI
        │  (NextAuth session)
        ▼
captify-pytology :4103     ← Cognito-gated gateway (validates the user JWT)
        │  Authorization: Bearer <user Cognito token>
        │  X-Api-Key: <DOCLING_SERVE_API_KEY>          (service auth)
        │  x-captify-tenant-id / x-captify-actor-id /  (caller identity)
        │  x-request-id
        ▼
docling-serve 127.0.0.1:3060   ← loopback-only bind, X-Api-Key enforced
        │  Authorization: Bearer <docling LiteLLM virtual key>
        │  x-captify-tenant-id / x-captify-actor-id    (forwarded identity)
        │  user: <actor id>                            (vision calls)
        ▼
LiteLLM proxy :4000        ← owns Bedrock credentials, guardrails, spend logs
        ▼
Amazon Bedrock
```

Key properties:

- **docling-serve holds no Bedrock/IAM model credentials.** All model calls
  (vision passes, knowledge-graph extraction) go through the LiteLLM proxy
  with a service-scoped virtual key (model-scoped, opted out of the
  prompt-injection guardrail because docling sends document-derived
  content). See the LiteLLM section of `.env` for the key-generation recipe.
- **Cognito validation happens at the gateway**, not here. docling-serve
  trusts the `x-captify-*` identity headers only because the service is
  loopback-bound and gated by the shared `X-Api-Key` secret.
- **AWS access is S3-only.** The service IAM user is scoped to the
  deep-extraction bucket; request-supplied buckets must pass the explicit
  allow-list (`ensure_bucket_allowed`, a confused-deputy guard).

## Service authentication (X-Api-Key)

`DOCLING_SERVE_API_KEY` must be set. When it is empty, the upstream
`APIKeyAuth` dependency accepts **every** request — never run that way, even
on a loopback bind, because any local process could otherwise drive S3 reads
and model spend on the service's credentials.

- docling-serve: `DOCLING_SERVE_API_KEY=...` in `.env`.
- captify-pytology: the same value as `DOCLING_SERVE_API_KEY` in its env;
  `DoclingServeClient` sends it as `X-Api-Key` on every call.

## Caller identity flow

The gateway forwards three headers on every request:

| Header | Meaning | Setting (default) |
|---|---|---|
| `x-captify-tenant-id` | Tenant the request belongs to | `eng_ray_tenant_id_header` |
| `x-captify-actor-id` | User (or agent) that initiated the call | `actor_id_header` |
| `x-request-id` | Gateway correlation id | `request_id_header` |

> The historical default tenant header `X-Tenant-Id` was never sent by any
> caller, so tenancy silently fell back to `"default"`. The defaults now
> match what the gateway actually sends.

Flow through the service (`docling_serve/identity.py`):

1. **HTTP layer** (`app.py`): a `_caller_identity` dependency reads the
   headers into a `RequestIdentity` and stamps `tenant_id` / `actor_id` /
   `request_id` onto the task metadata at enqueue time.
2. **Worker** (`deep_document/export_results.py`): before bundle assembly,
   the identity is rebuilt from task metadata and bound to a `ContextVar`
   (`bind_identity`) for the duration of the extraction, so nothing needs to
   thread identity arguments through extractor signatures.
3. **Model transports** read `current_identity()`:
   - `providers/bedrock.py` (vision): adds `user=<actor id>` to the
     chat-completions body and forwards the identity headers.
   - `extractors/enhancers/graph_extraction.py` (docling-graph): forwards
     the identity headers via the docling-graph connection overrides.
4. **Synchronous endpoints** that call models directly (e.g.
   `POST /v1/graph/extract`) bind the identity from the request headers
   around the call.

## Spend attribution in LiteLLM

The LiteLLM proxy records the forwarded identity headers as spend-log tags
via `litellm_settings.extra_spend_tag_headers` (see
`lite-llm/litellm.runtime.yaml`):

```yaml
litellm_settings:
  extra_spend_tag_headers:
    - "x-captify-tenant-id"
    - "x-captify-actor-id"
```

Every model call initiated by a user-driven extraction therefore lands in
`/spend/logs` tagged like:

```
tags: ['x-captify-tenant-id: anautics', 'x-captify-actor-id: 7418a408-…']
```

Vision calls additionally carry the OpenAI `user` field (end-user spend
tracking), and `x-litellm-tags` marks the call type (`docling-vision`,
`docling-graph`). System-initiated work with no bound identity falls back to
plain service-key attribution.

## Verified end-to-end (2026-06-11)

PPTX/DOCX/XLSX notebook uploads through the Cognito-gated gateway as a real
user produced: chunking, knowledge-graph extraction, Neo4j projection
(entities/mentions), S3 extraction bundles — with the graph LLM calls tagged
`tenant:anautics` / `actor:<user sub>` in LiteLLM spend logs and the
matching embeddings tracked on the user's per-user virtual key.

Regression coverage: `tests/test_identity_attribution.py` (identity
round-trip, ContextVar scoping, provider payload/headers).

## Operational notes

- The pm2 process must be started with a clean environment (the supervised
  `scripts/start-service.sh` sources `.env` itself). A stale pm2 env
  snapshot once leaked a deleted LiteLLM key — and the LiteLLM **master**
  key — into the process; recreate the pm2 app from a clean shell rather
  than carrying over inherited env vars.
- Restart with `pm2 restart docling-serve` (the only sanctioned way; see
  `scripts/start-service.sh`).
