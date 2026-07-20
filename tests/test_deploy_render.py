import json
from pathlib import Path

import pytest
import yaml

from scripts.configure_upload_staging import configure, lifecycle_configuration
from scripts.render_deploy_example import (
    IMAGE_TOKEN,
    render_manifest,
    validate_immutable_image,
)

VALID_IMAGE = f"registry.example.com/captify/docling-serve@sha256:{'a' * 64}"
STAGING_ARGS = {
    "staging_bucket": "captify-docling-staging-prod",
    "staging_region": "us-gov-west-1",
    "staging_api_role_arn": ("arn:aws-us-gov:iam::123456789012:role/docling-api"),
    "staging_worker_role_arn": ("arn:aws-us-gov:iam::123456789012:role/docling-worker"),
    "staging_kms_key": (
        "arn:aws-us-gov:kms:us-gov-west-1:123456789012:"
        "key/12345678-1234-1234-1234-123456789abc"
    ),
}


@pytest.mark.parametrize(
    "image",
    [
        "",
        "docling-serve:latest",
        f"docling-serve@sha256:{'0' * 64}",
        f"docling-serve@sha256:{'A' * 64}",
        "docling-serve@sha256:short",
    ],
)
def test_render_rejects_missing_mutable_or_invalid_digest(image):
    with pytest.raises(ValueError):
        validate_immutable_image(image)


def test_all_kubernetes_examples_render_to_parseable_immutable_manifests():
    templates = [
        path
        for path in Path("docs/deploy-examples").glob("*.yaml")
        if IMAGE_TOKEN in path.read_text()
    ]
    assert templates
    for path in templates:
        rendered = render_manifest(path.read_text(), VALID_IMAGE, **STAGING_ARGS)
        assert IMAGE_TOKEN not in rendered
        documents = list(yaml.safe_load_all(rendered))
        assert documents
        images = [
            container["image"]
            for document in documents
            if isinstance(document, dict)
            for container in document.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
            if container.get("name") in {"api", "worker", "loader"}
        ]
        assert all(image == VALID_IMAGE for image in images)
        for document in documents:
            if not isinstance(document, dict):
                continue
            for container in (
                document.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            ):
                if container.get("name") in {"api", "worker"}:
                    resources = container.get("resources", {})
                    assert resources["requests"]["ephemeral-storage"]
                    assert resources["limits"]["ephemeral-storage"]


def test_service_account_role_placeholders_render_and_parse():
    path = Path("docs/deploy-examples/upload-staging-serviceaccounts.yaml")
    rendered = render_manifest(path.read_text(), VALID_IMAGE, **STAGING_ARGS)
    documents = list(yaml.safe_load_all(rendered))
    assert [item["metadata"]["name"] for item in documents] == [
        "docling-serve-api",
        "docling-serve-worker",
    ]


def test_renderer_rejects_missing_or_invalid_staging_values():
    template = Path("docs/deploy-examples/docling-serve-simple.yaml").read_text()
    with pytest.raises(ValueError):
        render_manifest(template, VALID_IMAGE)
    with pytest.raises(ValueError):
        render_manifest(
            template,
            VALID_IMAGE,
            **{**STAGING_ARGS, "staging_bucket": "INVALID_BUCKET"},
        )


def test_lifecycle_provisioner_applies_and_verifies_exact_rule():
    class Client:
        def __init__(self):
            self.unrelated = [
                {
                    "ID": "compliance-retention",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "records/"},
                    "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}],
                    "Expiration": {"Days": 2555},
                },
                {
                    "ID": "abort-other-multipart",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "other/"},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                },
            ]
            self.configuration = {"Rules": self.unrelated.copy()}
            self.policy = {"Version": "2012-10-17", "Statement": []}
            self.put_count = 0

        def put_bucket_lifecycle_configuration(self, **kwargs):
            self.put_count += 1
            self.configuration = kwargs["LifecycleConfiguration"]

        def get_bucket_lifecycle_configuration(self, **kwargs):
            return self.configuration

        def get_bucket_policy(self, **kwargs):
            return {"Policy": json.dumps(self.policy)}

    client = Client()
    before = json.dumps(client.unrelated, sort_keys=True)
    configure(
        client,
        bucket="captify-docling-staging-prod",
        retention_days=1,
        check_only=False,
        expected_bucket_policy=client.policy,
    )
    assert client.configuration["Rules"][-4:] == lifecycle_configuration(1)["Rules"]
    assert json.dumps(client.configuration["Rules"][:-4], sort_keys=True) == before
    configure(
        client,
        bucket="captify-docling-staging-prod",
        retention_days=1,
        check_only=True,
        expected_bucket_policy=client.policy,
    )
    assert client.put_count == 1
    with pytest.raises(ValueError):
        lifecycle_configuration(8)
    assert json.loads(
        Path("docs/deploy-examples/upload-staging-lifecycle.json").read_text()
    ) == lifecycle_configuration(1)


def test_lifecycle_provisioner_rejects_conflict_without_safe_migration():
    conflict = {
        "ID": "legacy-docling-staging",
        "Status": "Enabled",
        "Filter": {"Prefix": "docling-staging/v1/"},
        "Expiration": {"Days": 30},
    }

    class Client:
        configuration = {"Rules": [conflict]}

        def get_bucket_lifecycle_configuration(self, **kwargs):
            return self.configuration

        def put_bucket_lifecycle_configuration(self, **kwargs):
            self.configuration = kwargs["LifecycleConfiguration"]

    client = Client()
    with pytest.raises(RuntimeError, match="safe-migration"):
        configure(
            client,
            bucket="bucket",
            retention_days=1,
            check_only=False,
        )
    assert client.configuration == {"Rules": [conflict]}

    configure(
        client,
        bucket="bucket",
        retention_days=1,
        check_only=False,
        allow_safe_migration=True,
    )
    assert client.configuration == lifecycle_configuration(1)


def test_image_workflow_uses_installed_runtime_staging_smoke():
    workflow = Path(".github/workflows/job-image.yml").read_text()
    assert "python -m docling_serve.staging_smoke" in workflow
    assert "pytest -q tests/test_upload_staging_live.py" not in workflow
    containerfile = Path("Containerfile").read_text()
    assert "COPY --chown=1001:0 ./tests" not in containerfile


def test_runtime_staging_smoke_cannot_report_success_when_disabled(monkeypatch):
    from docling_serve import staging_smoke

    monkeypatch.setattr(
        staging_smoke.docling_serve_settings, "upload_staging_mode", "disabled"
    )
    with pytest.raises(RuntimeError, match="mode=required"):
        staging_smoke.main()

    called = []
    monkeypatch.setattr(
        staging_smoke.docling_serve_settings, "upload_staging_mode", "required"
    )
    monkeypatch.setattr(
        staging_smoke,
        "check_upload_staging_capability",
        lambda **kwargs: called.append(kwargs),
    )
    staging_smoke.main()
    assert called == [{"force": True}]


@pytest.mark.parametrize(
    "template_name",
    [
        "upload-staging-api-iam-policy.json.template",
        "upload-staging-worker-iam-policy.json.template",
    ],
)
def test_staging_iam_lists_only_exact_cleanup_queue_prefix(template_name):
    policy = json.loads(Path(f"docs/deploy-examples/{template_name}").read_text())
    list_statements = [
        statement
        for statement in policy["Statement"]
        if statement["Action"] == "s3:ListBucket"
    ]
    assert len(list_statements) == 1
    statement = list_statements[0]
    assert statement["Resource"] == ("arn:aws:s3:::DOCLING_STAGING_BUCKET_PLACEHOLDER")
    assert statement["Condition"] == {
        "StringLike": {
            "s3:prefix": [
                "docling-staging-cleanup/v1/queue/",
                "docling-staging-cleanup/v1/queue/*",
            ]
        }
    }
    serialized = json.dumps(statement)
    assert "docling-staging-cleanup/v1/*" not in serialized

    cleanup_statement = next(
        item
        for item in policy["Statement"]
        if item["Sid"] in {"ReconcileEncryptedCleanupQueue", "WriteCleanupQueueState"}
    )
    assert cleanup_statement["Resource"] == [
        "arn:aws:s3:::DOCLING_STAGING_BUCKET_PLACEHOLDER/"
        "docling-staging-cleanup/v1/queue/*",
        "arn:aws:s3:::DOCLING_STAGING_BUCKET_PLACEHOLDER/"
        "docling-staging-cleanup/v1/dead/*",
        "arn:aws:s3:::DOCLING_STAGING_BUCKET_PLACEHOLDER/"
        "docling-staging-cleanup/v1/claims/*",
    ]
