# Rendering deployment examples

Kubernetes examples use `DOCLING_SERVE_IMAGE_PLACEHOLDER` and must be rendered
with an immutable image built from this repository's `Containerfile`:

```bash
uv run python scripts/render_deploy_example.py \
  --image registry.example.com/captify/docling-serve@sha256:<64-hex-digest> \
  --staging-bucket captify-docling-staging-prod \
  --staging-region us-gov-west-1 \
  --staging-kms-key arn:aws-us-gov:kms:us-gov-west-1:123456789012:key:<uuid> \
  --staging-api-role-arn arn:aws-us-gov:iam::123456789012:role/docling-api \
  --staging-worker-role-arn arn:aws-us-gov:iam::123456789012:role/docling-worker \
  --input docs/deploy-examples/docling-serve-rq-workers.yaml \
  --output /tmp/docling-serve-rq-workers.yaml
kubectl apply -f /tmp/docling-serve-rq-workers.yaml
```

The renderer fails closed for missing, mutable, malformed, or all-zero digests.
Compose examples use the required `DOCLING_SERVE_IMAGE` environment variable and
likewise expect a `repository@sha256:digest` value.

## Captify receiver authentication

Captify production deployments must load `captify-assertion.env.example` with
real KMS and shared Redis values. Assertion mode verifies a single request-bound
RS256 assertion and never falls back to `X-Api-Key`. Generic non-Captify
deployments may explicitly select `api_key` mode instead.

## Upload staging

Production images run with `DOCLING_SERVE_UPLOAD_STAGING_MODE=required`. Multipart
uploads are written to the fixed `docling-staging/v1/` prefix using ambient IAM;
no access key, presigned URL, or query signature is stored in a task. Apply and
verify the hard lifecycle backstop before deploying:

```bash
uv run python scripts/configure_upload_staging.py \
  --bucket captify-docling-staging-prod \
  --region us-gov-west-1 \
  --retention-days 1 \
  --cleanup-retention-days 7 \
  --dead-letter-retention-days 30 \
  --claim-retention-days 1 \
  --bucket-policy /path/to/rendered-staging-bucket-policy.json
```

The exact lifecycle document is `upload-staging-lifecycle.json`. IAM templates
for split API and worker roles are
`upload-staging-api-iam-policy.json.template` and
`upload-staging-worker-iam-policy.json.template`. Apply
`upload-staging-serviceaccounts.yaml` after rendering its role placeholders.
RQ should use separate roles. Local and Ray execute conversion in the API/Serve
pod, so their pod role must combine the API and worker statements.

The provisioner reads the current lifecycle and upserts only the four stable
source, cleanup-queue, dead-letter, and claim rules; unrelated compliance,
transition, and retention rules are preserved. Conflicting legacy staging rules
fail closed.
After review, `--allow-safe-migration` removes only those detected conflicts.
When `--bucket-policy` is supplied, the existing dedicated bucket policy is
verified semantically and is never replaced.

Failed immediate deletes are written as extra-forbidden cleanup records under
`docling-staging-cleanup/v1/`, protected by the configured bucket SSE policy.
The API reconciler processes bounded due batches independent of task polling,
reschedules transient failures with attempt/next-at/error-code state, and keeps
permanent failures under the encrypted dead-letter prefix for a finite audit
period. Queue mutations use conditional S3 claim objects under
`docling-staging-cleanup/v1/claims/`; ETag fencing prevents competing replicas
from transitioning the same record, while expired claims are reclaimable after
a crash. Readiness checks queue listing plus conditional claim/release.

KMS encryption is preferred and required by these production examples. If an
operator deliberately leaves `DOCLING_SERVE_UPLOAD_STAGING_KMS_KEY_ID` empty,
the service uses S3-managed AES-256 encryption and readiness verifies it on the
canary. `disabled` mode exposes URL-only conversion and returns 503 from every
multipart file-upload endpoint; it is only for explicit local/test deployments.
