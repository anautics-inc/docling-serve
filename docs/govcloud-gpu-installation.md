# GovCloud GPU installation

This runbook documents the existing production path in `.gitlab-ci.yml`.
GitLab builds and scans Docling Serve, assumes a role in AWS GovCloud, and uses
SSM Run Command to replace the GPU-enabled Docker container on existing EC2
instances. The pipeline is the installer; do not run a second installation
method beside it.

## Ownership boundary

This repository consumes, but does not provision:

- the GovCloud account, `us-gov-west-1` network, DNS, and edge TLS;
- SSM-managed GPU EC2 instances and `ansible-ssm-target-role`;
- Docker, the NVIDIA driver, and NVIDIA Container Toolkit on each host;
- the dedicated S3 staging bucket and KMS keys;
- IAM policies for the deployment role and EC2 instance role;
- TLS Redis for assertion replay protection and for RQ only when separately
  deployed and selected;
- private LiteLLM connectivity when remote-model features are enabled.

The current production job starts one `docling-serve` GPU container. Its
production environment should therefore use `DOCLING_SERVE_ENG_KIND=local`
unless RQ workers are owned and operated by a separate approved deployment.

## GitLab production variables

Configure these as protected variables:

- `PRODUCTION_ACCOUNT_ID`: 12-digit GovCloud account id.
- `PRODUCTION_INSTANCE_IDS`: space-separated target EC2 instance ids.
- `PRODUCTION_DOMAIN`: approved service domain recorded by the deployment.
- `PRODUCTION_ENV`: protected file variable rendered from
  `docs/deploy-examples/govcloud-production.env.example`.
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`: masked base credentials used
  by the runner to assume the deployment role. Prefer the organization-approved
  short-lived runner identity when available.

The pipeline supplies GitLab registry credentials and defaults
`AWS_REGION=us-gov-west-1`, `HOST_PORT=8000`, and `CONTAINER_PORT=5001`.
Do not put credentials in the repository or print the rendered deployment
script.

## Render and review AWS policies

Render the combined single-container runtime policy:

```bash
uv run python scripts/render_deploy_example.py \
  --image registry.example/docling-serve@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --staging-bucket <bucket> \
  --staging-region us-gov-west-1 \
  --staging-kms-key arn:aws-us-gov:kms:us-gov-west-1:<account>:key/<uuid> \
  --assertion-kms-key arn:aws-us-gov:kms:us-gov-west-1:<account>:key/<uuid> \
  --input docs/deploy-examples/govcloud-runtime-iam-policy.json.template \
  --output /tmp/docling-runtime-policy.json
```

Render the dedicated bucket policy with the same bucket, region, and staging
key, using
`docs/deploy-examples/upload-staging-bucket-policy.json.template`. Review both
documents before applying them. In GovCloud, every S3 resource must begin with
`arn:aws-us-gov:s3:::`. The Docling instance role receives
`kms:GetPublicKey`; the Pytology assertion signer, not Docling, receives
`kms:Sign`.

Apply and verify the lifecycle without replacing unrelated rules:

```bash
uv run python scripts/configure_upload_staging.py \
  --bucket <bucket> \
  --region us-gov-west-1 \
  --retention-days 1 \
  --cleanup-retention-days 7 \
  --dead-letter-retention-days 30 \
  --claim-retention-days 1 \
  --bucket-policy /tmp/docling-staging-bucket-policy.json
```

## Read-only preflight

Run from the GitLab runner or an approved administration host:

```bash
aws sts get-caller-identity --region us-gov-west-1
aws ssm describe-instance-information \
  --region us-gov-west-1 \
  --query 'InstanceInformationList[].[InstanceId,PingStatus,PlatformName,AgentVersion]'
aws s3api get-bucket-location --bucket <bucket> --region us-gov-west-1
aws s3api get-bucket-lifecycle-configuration \
  --bucket <bucket> --region us-gov-west-1
aws s3api get-bucket-policy --bucket <bucket> --region us-gov-west-1
aws kms describe-key --key-id <staging-key-arn> --region us-gov-west-1
aws kms get-public-key --key-id <assertion-key-arn> --region us-gov-west-1
```

Use SSM Run Command to verify each target host without changing it:

```bash
nvidia-smi
docker version
docker info
df -h / /mnt/docling/scratch
getent hosts <redis-host>
timeout 5 bash -c '</dev/tcp/<redis-host>/6379'
```

Confirm the security groups, routes, and private DNS paths for S3/KMS, Redis,
the GitLab registry, CloudWatch/OTEL collectors, and LiteLLM when enabled.
Confirm the production env has no `__PLACEHOLDER__` values and passes the
pipeline guards for production mode, assertion auth, required staging, staging
KMS, explicit engine, and `/usr/bin/soffice`.

## Initial deployment and update

1. Select the approved commit on `main` and confirm its build, unit, SAST,
   dependency, container-scan, and SBOM jobs passed.
2. Record the commit SHA, GitLab image reference/digest, current running image,
   target instances, enabled optional capabilities, and rollback commit.
3. Run the manual `deploy-production` job.
4. The existing job assumes the GovCloud role, verifies SSM Online, pulls the
   pipeline image, recreates `docling-serve` with `--gpus all`, mounts
   `/mnt/docling/scratch`, and waits for `/ready` plus `/ready/adapters`.
5. Do not approve the change solely because the container started. Complete the
   acceptance checks below.

## Acceptance

Check the deployed service:

```bash
curl --fail --silent https://<service-domain>/health
curl --fail --silent https://<service-domain>/ready
curl --fail --silent https://<service-domain>/ready/adapters
curl --fail --silent https://<service-domain>/v1/capabilities
```

Run authenticated canonical acceptance from an identity allowed to sign with
the acceptance KMS key:

```bash
export DOCLING_SERVE_ACCEPTANCE_ASSERTION_KMS_KEY_ID=<signing-key-arn>
export DOCLING_SERVE_ACCEPTANCE_ASSERTION_KMS_REGION=us-gov-west-1
export DOCLING_SERVE_ACCEPTANCE_TENANT_ID=acceptance
export DOCLING_SERVE_ACCEPTANCE_DOCUMENT_ID=docling-acceptance-<change-id>
uv run python scripts/verify_post_deploy.py https://<service-domain>
```

Execute existing live tiers required by enabled production policy:

- `tests/test_upload_staging_live.py` for credentialed S3/KMS staging;
- `tests/test_gpu_live.py` and model endpoint tests on the target GPU class;
- `tests/test_rq_live.py` only when a separately managed RQ topology is enabled;
- `tests/test_kicad_live.py` when KiCad export/ERC is required;
- `scripts/verify_production_samples.py` for approved sample documents;
- the Pytology checklist in `captify-pytology-todos.md`.

A job that is merely scheduled, skipped, or missing credentials is pending
evidence, not a pass.

## Logs and release evidence

Collect the GitLab security package, merged build/runtime SBOMs, SAST report,
container scan, deployment SSM command id/status, readiness responses,
post-deploy output, live-tier results, and Pytology downstream evidence. Tie the
record to the Docling commit and deployed image reference.

The container writes to Docker's configured log driver. Ensure the host forwards
logs to the approved GovCloud destination and has host-level log rotation and
disk alarms. Scrape `/metrics` or configure OTLP according to the environment.

## Rollback

The existing deployment removes stale local images, so rollback depends on the
recorded approved GitLab revision, not an unrecorded host tag.

1. Stop further submissions at the upstream gateway.
2. Record the failed image, container logs, readiness output, and task ids.
3. Re-run the existing pipeline for the previously approved commit so its
   branch image is rebuilt and deployed through the same SSM production job.
4. Verify `/ready` and `/ready/adapters`.
5. Run `scripts/verify_post_deploy.py` with a new acceptance document id.
6. Re-enable submissions only after canonical acceptance and required live
   tiers pass.

If the old revision cannot be rebuilt and retrieved from the GitLab registry,
rollback is blocked; escalate rather than running an unreviewed host-local
installation.
