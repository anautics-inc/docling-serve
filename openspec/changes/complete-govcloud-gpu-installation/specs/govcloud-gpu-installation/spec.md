# Spec Delta: GovCloud GPU installation

## ADDED Requirements

### Requirement: The existing GitLab pipeline is the installation boundary

Production installation SHALL use the repository's existing GitLab build,
security-package, GovCloud role-assumption, SSM, and GPU Docker deployment
stages. Installation artifacts SHALL NOT require a second infrastructure or
orchestration path.

#### Scenario: An operator deploys the production environment

- **WHEN** the protected production job runs from the approved branch
- **THEN** it uses the existing `.gitlab-ci.yml` path and the approved
  `PRODUCTION_*` variables without a separate manual installer

### Requirement: External prerequisites are verified before deployment

The installation contract SHALL identify the existing GovCloud account, region,
SSM-managed EC2 targets, deployment role, NVIDIA runtime, Docker runtime,
scratch capacity, S3 staging bucket, KMS keys, configured Redis, network paths,
and edge TLS as external prerequisites.

#### Scenario: A target instance is not SSM Online

- **WHEN** preflight cannot confirm an intended target is SSM Online
- **THEN** deployment remains blocked before the running container is changed

#### Scenario: The GPU runtime is unavailable

- **WHEN** `nvidia-smi` or the NVIDIA Docker runtime check fails
- **THEN** installation fails without claiming GPU model readiness

### Requirement: Deployment policies use the target AWS partition

Rendered IAM and bucket policies SHALL use `aws-us-gov` for GovCloud resources
and SHALL NOT emit commercial `arn:aws` S3 resources for a `us-gov-*` target.

#### Scenario: Staging policies target us-gov-west-1

- **WHEN** policy examples are rendered for `us-gov-west-1`
- **THEN** every staging bucket and object resource begins with
  `arn:aws-us-gov:s3:::`

### Requirement: Runtime IAM is least privilege and complete

The documented runtime role SHALL scope S3 access to the staging and cleanup
prefixes, KMS data-key operations to the staging key, and
`kms:GetPublicKey` to the configured asymmetric assertion key.

#### Scenario: Assertion verification uses a KMS key

- **WHEN** `DOCLING_SERVE_ASSERTION_KMS_KEY_ID` is configured
- **THEN** the runtime identity can call `kms:GetPublicKey` for that key without
  receiving signing or broad KMS administration permissions

### Requirement: The staging bucket enforces transport and encryption

The dedicated staging bucket policy SHALL deny non-TLS requests and SHALL deny
staged object writes that do not request the configured KMS key.

#### Scenario: A client attempts an unencrypted staging write

- **WHEN** `PutObject` omits KMS encryption or selects a different KMS key
- **THEN** the bucket policy denies the write

### Requirement: Production configuration is explicit and fail closed

The protected production environment SHALL select production mode, assertion
authentication, no default tenant, explicit CORS, required upload staging with
GovCloud region and KMS key, an explicit execution engine, the installed
LibreOffice path, and explicit remote-model capability flags.

#### Scenario: A required production value is still a template placeholder

- **WHEN** an operator prepares `PRODUCTION_ENV` with an unresolved placeholder
- **THEN** preflight rejects the file before it is used for deployment

### Requirement: Installation acceptance is evidence based

The operator SHALL verify `/ready`, `/ready/adapters`, an authenticated
canonical submit/poll/result/cleanup lifecycle, and every enabled live
capability tier against the candidate deployment.

#### Scenario: A scheduled live tier has no current result

- **WHEN** the candidate revision has no current passing evidence for an enabled
  GPU, S3/KMS, Redis, KiCad, model, or production-sample tier
- **THEN** the installation record marks that tier pending and does not infer a
  pass from hermetic tests

### Requirement: Rollback uses an approved previous image

Before production deployment, the operator SHALL record the previously
approved image reference and SHALL verify readiness and authenticated canonical
acceptance after restoring it.

#### Scenario: Post-deploy acceptance fails

- **WHEN** the new container starts but authenticated canonical acceptance fails
- **THEN** the operator restores the recorded image through the established
  deployment path and repeats readiness and acceptance checks
