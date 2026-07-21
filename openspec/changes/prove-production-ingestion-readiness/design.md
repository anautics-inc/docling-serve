# Design: Prove production ingestion readiness

## Evidence model

Every production surface is assigned one validation tier in the path ledger.
Merge evidence proves deterministic contracts. Container evidence proves
packaging and external executables. Distributed evidence proves queue and
cluster semantics. Credentialed evidence proves cloud policy. GPU/model and
production-sample evidence prove quality on the deployed runtime. Post-deploy
evidence proves the request-bound service contract through Pytology.

An unavailable required capability is not a passing result. It must either
block release or be disabled in deployment policy and reported unavailable by
`/ready/adapters`.

## Cross-service contract

Pytology owns source identity, request-bound assertion minting, canonical task
submission, polling, result validation, cleanup, entity extraction,
OpenSearch indexing, and Neo4j projection. Docling Serve owns admission,
routing, conversion, chunking, typed extraction, and typed artifact
publication. Pytology must not reimplement typed extraction or accept a
non-canonical result for canonical ingestion.

Each authenticated request binds method, exact path, tenant, document, client,
expiry, and one-time assertion ID. Poll and result requests receive new
assertions because assertions are path-bound and replay-protected.

## Runtime policy

- RQ requires a reachable Redis URL and proves enqueue, execution, result TTL,
  failure TTL, trace propagation, staging cleanup, and canonical decoration.
- Ray requires an address and Redis coordination, and proves coordinator
  decoration, retries, page slicing, failure publication, and Local/RQ/Ray
  result parity.
- S3 staging requires TLS, fixed prefixes, KMS policy, lifecycle policy,
  integrity metadata, canary operations, and cleanup.
- GPU/model validation runs only on the production accelerator class.
- KiCad readiness reports core schematic extraction separately from optional
  export/ERC. Deployment policy decides whether missing KiCad blocks release.
- Production samples must use new source bytes or identities that cannot reuse
  a previously completed canonical job.

## Pytology handoff

The repository root file `captify-pytology-todos.md` is the authoritative
consumer handoff. It contains only Pytology-owned changes, exact Docling
contracts, acceptance tests, configuration, and release criteria.

## Release decision

The release is production-ready only when all enabled critical tiers have
current evidence from the candidate revision. Scheduled jobs without a passing
run are pending evidence, not validation.
