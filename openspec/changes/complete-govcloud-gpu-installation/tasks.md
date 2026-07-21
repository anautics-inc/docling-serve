# Tasks: Complete GovCloud GPU installation

## 1. Specification

- [x] 1.1 [GGI-1] Add proposal, alternatives, decision log, requirements, and
  traced tasks for the existing GitLab/SSM/GPU installation.
- [x] 1.2 [GGI-7] Modify production-readiness requirements so missing scheduled
  evidence remains pending.
- [x] 1.3 [GGI-7] Add the missing Pytology consumer handoff.

## 2. GovCloud policy artifacts

- [x] 2.1 [GGI-3] Render staging S3 IAM resources with the target AWS partition.
- [x] 2.2 [GGI-4] Add a complete least-privilege GovCloud runtime policy.
- [x] 2.3 [GGI-5] Add and render the dedicated TLS/KMS staging bucket policy.
- Blocked by: task 1.1.

## 3. Configuration and operations

- [x] 3.1 [GGI-2,GGI-6] Add the reviewed production environment template.
- [x] 3.2 [GGI-1,GGI-2,GGI-7,GGI-8] Add the existing-pipeline installation,
  acceptance, and rollback runbook.
- [x] 3.3 [GGI-2] Document read-only GovCloud, host, GPU, storage, identity,
  Redis, binary, disk, and network preflight.
- Blocked by: tasks 2.1 through 2.3.

## 4. Verification

- [x] 4.1 [GGI-3,GGI-4,GGI-5] Add policy and renderer contract tests.
- [x] 4.2 [GGI-6] Add environment and runbook parity tests against existing
  GitLab guards.
- [x] 4.3 Run `npx --yes @fission-ai/openspec@latest validate --all --strict`.
- [x] 4.4 Run Ruff/formatting for changed Python, targeted deployment/auth/
  staging tests, and the full test suite where practical.
- [x] 4.5 Run available existing container/GPU/S3/KMS/KiCad/sample/post-deploy
  acceptance commands and record unavailable live tiers as pending.
- [x] 4.6 Verify `.gitlab-ci.yml` is byte-for-byte unchanged.
