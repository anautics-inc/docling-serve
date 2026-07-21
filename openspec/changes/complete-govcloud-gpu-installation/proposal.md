# Proposal: Complete GovCloud GPU installation

## Why

Docling Serve already has a GitLab production pipeline that builds, scans, and
deploys the GPU image to existing AWS GovCloud EC2 instances through SSM. The
application enforces production authentication and KMS-backed upload staging,
but the repository does not yet define the complete installation contract for
that pipeline. Its example S3 IAM policies also use the commercial AWS
partition, and the required bucket, runtime-role, environment, acceptance, and
rollback artifacts are incomplete.

## What Changes

- Make the existing `.gitlab-ci.yml` deployment the authoritative GovCloud GPU
  installation path without changing its behavior.
- Specify every external prerequisite, protected GitLab variable, host
  prerequisite, runtime setting, readiness check, acceptance tier, and rollback
  record required by that path.
- Correct staging IAM resources for the `aws-us-gov` partition and add
  least-privilege runtime and bucket-policy examples.
- Add one production environment template and one operator runbook aligned with
  the pipeline's existing guards.
- Complete the Pytology consumer handoff required by the production-ingestion
  readiness design.

## Capabilities

### New Capabilities

- `govcloud-gpu-installation`

### Modified Capabilities

- `production-ingestion-readiness`
- Captify Pytology `canonical-document-ingest`

## Impact

- `docling-serve`: OpenSpec, IAM/bucket policy examples, deployment rendering
  tests, production environment template, and operator documentation.
- `captify-pytology`: documented assertion, canonical lifecycle, search, NER,
  OpenSearch, and Neo4j acceptance ownership; no code changes in this change.
- Public HTTP and bundle contracts are unchanged.

## Non-goals

- Editing `.gitlab-ci.yml`.
- Provisioning GovCloud resources with Terraform, CloudFormation, or EKS.
- Replacing the GitLab registry, SSM deployment, GPU EC2 hosts, or Docker
  runtime.
- Adding a new Compose/RQ topology or changing the engine selected by
  `PRODUCTION_ENV`.
- Resolving branch-history or pull-request merge conflicts.

## Open Questions

None.
