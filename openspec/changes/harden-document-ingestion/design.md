# Design: Harden document ingestion

## Context

Format knowledge currently appears in Docling's registry, route-local checks,
legacy conversion maps, typed extractors, and Pytology's copied heuristics.
Operational behavior is similarly spread across settings and deployment files.

## Approaches considered

### A. Keep client-side routing and consolidate constants

Move constants into shared files but retain Pytology's content heuristics and
route selection. This is low-risk initially, but two deployable services still
decide document semantics independently and can drift.

### B. Service-owned capability registry and auto extraction

Docling Serve owns format admission, OCR policy, typed probes, route selection,
runtime readiness, and output-contract metadata. Clients either force an
explicit profile or ask the service to choose. Compatibility fields and
explicit routes remain during migration.

Selected: **B**. It removes semantic duplication without coupling clients to
Docling's Python internals and allows capability behavior to be tested once.

## Architecture

1. `DocumentCapabilityRegistry` describes generic and typed document families.
2. Pure probes classify a source using suffix, MIME type, bounded bytes, and
   optionally converted markdown.
3. `/v1/extract/auto` returns the selected domain, reason, and typed result.
4. Existing `/v1/extract/*` routes delegate to the same registered adapters.
5. `/ready/adapters` reports per-capability availability without making an
   optional adapter outage fail unrelated formats.
6. Clients use typed OCR policy (`auto`, `always`, `never`) with legacy form
   fields translated at the API boundary.

## Configuration

Deployment and policy values use validated settings. Protocol values—MIME
types, schema identifiers, endpoint names, and security claim names—remain
versioned constants. Production validation rejects missing tenant scope,
unauthenticated exposure, wildcard CORS, or enabled model passes without
configured transport and budgets.

## Legacy Office isolation

The legacy adapter is split into source policy/materialization, sandboxed
LibreOffice execution, and result mapping. It retains SSRF protection,
executable allowlisting, process-group termination, time/size/file-count
limits, and stable public failures. Its readiness is optional and granular.

## Dependency policy

Upgrade coherent dependency families together and test all supported platform
groups. Absolute-latest versions that violate an upstream platform constraint
are documented as explicit compatibility exceptions. Container bases, actions,
and downloaded archives are pinned and verified.

## Testing seams

- Registry lookup and pure domain probes.
- OCR compatibility translation.
- FastAPI submit, auto-extract, status, result, and readiness handlers.
- Local/RQ orchestrator adapter boundaries.
- Legacy source materializer and subprocess sandbox.
- Pytology and Captify Core HTTP clients.
- Published extraction bundle parsers.
- Offline production container startup.

## Decision log

- Keep legacy Office support, but isolate it rather than run an unbounded subprocess.
- Keep stable explicit endpoints and add auto extraction for migration safety.
- Require tenant headers only for authenticated document/task operations; public
  health and explicitly local development remain usable.
- Default optional remote-model work off; enablement requires explicit policy.
- Prefer latest verified-compatible versions over an unresolvable “latest everywhere” lock.
