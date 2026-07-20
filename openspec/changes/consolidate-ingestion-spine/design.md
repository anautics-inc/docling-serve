# Design: Consolidate the ingestion spine

## Current state

The canonical worker converges on one indexing pipeline, but source preparation
has four owners: Pytology Access rendering, Pytology's optional Docling library
backend, Docling conversion tasks, and synchronous typed extraction routes.
This produces two parsing passes and allows content used for search to differ
from content used for typed artifacts.

## Selected design

Introduce a canonical Docling ingestion result produced by the existing
asynchronous task boundary. The result contains normalized markdown, chunks,
routing metadata, and optional typed artifact metadata. Executable adapters
prepare format-specific sources inside Docling Serve, while one task envelope
performs conversion and chunking. Pytology only submits, polls, validates, and
passes normalized chunks into its one entity/index/projection spine.

Typed HTTP routes remain available, but call the same domain services used by
the task rather than owning extraction implementations.

## Ordering

1. Remove dead and test-only layers.
2. Consolidate repeated Docling helpers without changing behavior.
3. Add the canonical result envelope and task-owned adapter dispatch.
4. Migrate Pytology Access and typed work to the task result.
5. Remove the in-process library backend and backend-selection surface.
6. Add hermetic format-matrix tests and an environment-gated live matrix.
7. Ratchet Graphify ceilings after all behavioral gates pass.

## Failure semantics

- Admission, conversion, and chunking failures fail the Docling task.
- Requested typed extraction failures fail the task.
- Automatic optional typed enrichment records a typed failure without losing
  valid generic chunks unless the selected domain requires typed artifacts.
- When entity extraction is configured as required, extraction failure or an
  unavailable provider prevents completion.
- OpenSearch and Neo4j checkpoint evidence remain mandatory.

## Reduction policy

- Delete code with no production or documented external consumer.
- Migrate internal consumers before deleting temporary facades.
- Do not merge cohesive schematic or technical-order modules merely to reduce
  file count.
- Prefer deleting duplicate ownership over moving code into additional files.

## Verification

- A hermetic synthetic corpus covers every admitted extension and routing
  family through the canonical result contract.
- A live environment-gated corpus verifies real Docling conversion,
  OpenSearch retrieval, entity evidence, and Neo4j document/chunk projection.
- Graphify gates zero Docling file cycles and ratchets both repositories'
  coupling metrics.

## Jobkit compatibility

`docling-jobkit` 2.x closes `DoclingTaskResult.result` over its built-in
discriminated result models, so a new top-level canonical result variant cannot
round-trip through Local and RQ storage. The canonical envelope is therefore
stored under the existing chunk result's extensible `chunking_info` field and
projected as the public result response. Local and RQ execute the same
preparation/typed-dispatch code. Ray's split converter/chunker deployments do
not expose a safe result-decoration hook, so canonical submissions fail closed
with 503 on Ray rather than returning a non-canonical success payload.
