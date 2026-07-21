# Spec Delta: Production ingestion readiness

## MODIFIED Requirements

### Requirement: Required runtime capabilities fail closed

Every enabled production capability SHALL have current evidence from the
candidate revision at its assigned release tier. An unavailable capability
SHALL block release unless deployment policy explicitly disables it. A
scheduled job without a current result SHALL be reported as pending evidence
and SHALL NOT be treated as a passing gate.

#### Scenario: GPU hardware is unavailable

- **WHEN** GPU-backed model processing is enabled for the target deployment
- **THEN** release remains blocked until the GPU/model acceptance job passes on
  the target accelerator class

#### Scenario: A scheduled acceptance job has not run

- **WHEN** a required credentialed or hardware acceptance job exists but has no
  result for the candidate revision
- **THEN** the release decision reports pending evidence rather than success

### Requirement: Credentialed staging proves cloud controls

Required S3 staging SHALL prove the target AWS partition, TLS, fixed prefix, KMS
enforcement, object integrity metadata, lifecycle policy, canary operations,
and cleanup using the target deployment identity.

#### Scenario: A GovCloud policy contains a commercial S3 ARN

- **WHEN** credentialed staging targets a `us-gov-*` region but its IAM or
  bucket policy contains an `arn:aws:s3` resource
- **THEN** policy validation and release acceptance fail closed
