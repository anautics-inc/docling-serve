# Captify Pytology handoff

This file is the authoritative list of Pytology-owned work required to accept a
Docling Serve production release. Docling Serve owns admission, routing,
conversion, chunking, typed extraction, and typed artifact publication.
Pytology owns source identity, request assertions, canonical lifecycle
orchestration, and downstream completion.

## Required request contract

- Mint a new RS256 assertion for every protected Docling request.
- Bind `iss`, `aud`, `sub`, `tid`, `cid`, `res`, `act`, `iat`, `nbf`, `exp`,
  and one-time `jti`; keep assertion lifetime at or below 300 seconds.
- Set the same tenant in `X-Captify-Tenant-Id` and `X-Tenant-Id`.
- Set `X-Captify-Document-Id` on submit, poll, result, and cleanup requests.
- Bind `res` exactly as
  `document:{tenant}:{document_id}:{request_path}` and `act` to the lowercase
  HTTP method.
- Never reuse a submit assertion for polling, result retrieval, or cleanup.

## Required canonical lifecycle

- [ ] Submit each admitted source through a canonical async convert or chunk
  endpoint and require `docling.canonical-ingestion.v1` in the result.
- [ ] Poll `/v1/status/poll/{task_id}` with a fresh assertion until a terminal
  status; retain tenant and document identity.
- [ ] Read `/v1/result/{task_id}` with a fresh assertion and reject a
  non-canonical result before downstream processing.
- [ ] Run result cleanup through the documented endpoint with a fresh assertion.
- [ ] Do not fall back to in-process Docling, Pytology-owned Access conversion,
  or a second typed-extraction request for a canonical task.

## Required downstream completion

- [ ] Validate and persist canonical chunks.
- [ ] Complete configured entity extraction/NER.
- [ ] Complete OpenSearch indexing.
- [ ] Complete Neo4j projection.
- [ ] Mark the document complete only after every required checkpoint succeeds;
  otherwise retain failed or retryable state.

## Production configuration

- Docling base URL resolves through the approved GovCloud network path.
- Assertion issuer is `captify-pytology`, audience is `docling-service`, and
  client id matches the receiver's configured value.
- The assertion signing identity can call `kms:Sign` only on the approved
  asymmetric key. Docling Serve receives only `kms:GetPublicKey`.
- HTTP timeouts cover submission and polling without exceeding Docling task
  policy; retry logic never reuses a `jti`.

## Release acceptance

- [ ] Submit new source bytes or a source identity that cannot resolve to a
  previously completed canonical job.
- [ ] Prove submit, poll, result, and cleanup with distinct request-bound
  assertions.
- [ ] Prove one generic document and every enabled typed domain.
- [ ] Prove chunk persistence, NER, OpenSearch searchability, and Neo4j
  projection before completion.
- [ ] Record the Docling Serve image reference, Docling commit, Pytology commit,
  tenant, document id, task id, enabled capabilities, and pass/fail evidence.

## Release criterion

Pytology integration is ready only when all checked behavior above passes
against the candidate Docling deployment. Missing credentials, unavailable
hardware, disabled services, or scheduled jobs that have not run are pending
evidence, not a pass.
