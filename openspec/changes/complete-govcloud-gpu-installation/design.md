# Design: Complete GovCloud GPU installation

## Context

The existing GitLab pipeline is already the deployed production mechanism. It
builds and scans the CUDA image, assumes an `aws-us-gov` deployment role,
verifies SSM connectivity, transmits the protected environment file, starts one
GPU-enabled container, and checks `/ready` and `/ready/adapters`. Existing
GovCloud EC2, S3, KMS, IAM, SSM, Redis when configured, network, and TLS
resources are owned outside this repository.

## Approaches considered

### A. Replace the installation with new infrastructure and orchestration

Introduce Terraform, EKS, ECR promotion, managed worker orchestration, and a new
release controller. This could improve long-term repeatability, but it would
replace a known production path, require ownership decisions outside this
repository, and expand the change beyond closing current installation gaps.

### B. Formalize and complete the existing GitLab/SSM installation

Treat `.gitlab-ci.yml` as a compatibility boundary. Correct the policies and
templates it consumes, define its external and runtime contracts, document its
actual operating procedure, and verify those artifacts without changing the
pipeline.

Selected: **B**. It preserves the established GovCloud GPU deployment while
making its prerequisites, security controls, acceptance evidence, and rollback
procedure reviewable.

## Installation contract

1. Protected GitLab production variables identify the existing GovCloud
   account, SSM-managed EC2 instances, service domain, and production env file.
2. The pipeline assumes the existing `ansible-ssm-target-role` in
   `us-gov-west-1`, verifies each target is SSM Online, and deploys the image
   built by that pipeline revision.
3. The host provides Docker, the NVIDIA driver and container toolkit, a writable
   scratch mount, network paths to configured dependencies, and enough disk for
   the current and rollback images.
4. The production environment selects assertion authentication, required
   KMS-backed upload staging, an explicit engine topology, explicit optional
   model capabilities, JSON logging, and no default tenant.
5. `/ready` proves mandatory dependencies; `/ready/adapters` records optional
   capability availability. Post-deploy acceptance proves an authenticated
   canonical task lifecycle.

## Policy rendering

Deployment policy templates carry an explicit partition placeholder. A
GovCloud region resolves it to `aws-us-gov`; a commercial region resolves it to
`aws`. S3 bucket and object ARNs omit region and account fields but retain the
selected partition.

The runtime role combines only the statements needed by the selected process:
upload staging operations, KMS use for staged objects, and `kms:GetPublicKey`
for assertion verification. The dedicated bucket policy denies non-TLS
requests and denies staged writes that do not use the configured KMS key.

## Production environment

The environment template is documentation, not a secret file. It contains no
credentials and uses conspicuous placeholders. GitLab's protected file variable
contains the actual values. Remote model capabilities remain disabled unless
the private LiteLLM route, secret, alias, budgets, timeout, retries, and usage
telemetry are configured.

## Acceptance and rollback

Merge-time tests prove settings, rendering, policy shape, and documentation
consistency. Existing container and scheduled jobs prove image packaging, GPU,
credentialed S3/KMS, optional KiCad, production samples, and post-deploy
behavior. A missing live tier is recorded as pending, not inferred to pass.

The existing pipeline removes stale service images after deployment, so the
operator records the previously approved GitLab image reference before
deployment. Rollback reruns the same production deployment path for that
reference or restores it on the host, then repeats readiness and authenticated
post-deploy acceptance.

## Testing seams

- GovCloud/commercial partition selection and placeholder rejection.
- Staging API/worker/runtime IAM action and resource scope.
- Bucket-policy TLS and KMS deny conditions.
- Production environment required keys and safe placeholders.
- Runbook parity with `.gitlab-ci.yml` production variables and guardrails.
- Existing production settings, staging, auth, deploy rendering, and
  post-deploy acceptance suites.

## Decision log

- Preserve `.gitlab-ci.yml` byte-for-byte.
- Preserve the existing single GPU-container deployment and engine selected by
  `PRODUCTION_ENV`.
- Treat infrastructure provisioning and edge TLS as external prerequisites.
- Correct invalid GovCloud IAM examples rather than accepting commercial ARNs.
- Do not claim a live acceptance tier passed when credentials or hardware are
  unavailable.
