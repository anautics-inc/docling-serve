"""Render a deployment example with a validated immutable project image."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

IMAGE_TOKEN = "DOCLING_SERVE_IMAGE_PLACEHOLDER"
STAGING_BUCKET_TOKEN = "DOCLING_STAGING_BUCKET_PLACEHOLDER"
STAGING_REGION_TOKEN = "DOCLING_STAGING_REGION_PLACEHOLDER"
STAGING_API_ROLE_ARN_TOKEN = "DOCLING_STAGING_API_ROLE_ARN_PLACEHOLDER"
STAGING_WORKER_ROLE_ARN_TOKEN = "DOCLING_STAGING_WORKER_ROLE_ARN_PLACEHOLDER"
STAGING_KMS_KEY_TOKEN = "DOCLING_STAGING_KMS_KEY_PLACEHOLDER"
_IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+@sha256:([0-9a-f]{64})$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
_ROLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov):iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$"
)
_KMS_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov):kms:[a-z0-9-]+:\d{12}:key/[0-9a-f-]{36}$"
)


def validate_immutable_image(image: str) -> str:
    match = _IMMUTABLE_IMAGE.fullmatch(image)
    if match is None or match.group(1) == "0" * 64:
        raise ValueError(
            "image must be a non-zero immutable reference: registry/repository@sha256:<64 lowercase hex>"
        )
    return image


def render_manifest(
    template: str,
    image: str,
    *,
    staging_bucket: str | None = None,
    staging_region: str | None = None,
    staging_api_role_arn: str | None = None,
    staging_worker_role_arn: str | None = None,
    staging_kms_key: str | None = None,
) -> str:
    rendered = template
    if IMAGE_TOKEN in rendered:
        validate_immutable_image(image)
        rendered = rendered.replace(IMAGE_TOKEN, image)
    replacements = {
        STAGING_BUCKET_TOKEN: (staging_bucket, _BUCKET),
        STAGING_REGION_TOKEN: (staging_region, _REGION),
        STAGING_API_ROLE_ARN_TOKEN: (staging_api_role_arn, _ROLE_ARN),
        STAGING_WORKER_ROLE_ARN_TOKEN: (staging_worker_role_arn, _ROLE_ARN),
        STAGING_KMS_KEY_TOKEN: (staging_kms_key, _KMS_ARN),
    }
    for token, (value, pattern) in replacements.items():
        if token not in rendered:
            continue
        if value is None or pattern.fullmatch(value) is None:
            raise ValueError(f"valid value is required for {token}")
        rendered = rendered.replace(token, value)
    if IMAGE_TOKEN in rendered:
        raise ValueError("image placeholder was not fully rendered")
    remaining = [token for token in replacements if token in rendered]
    if remaining:
        raise ValueError(f"staging placeholders were not fully rendered: {remaining}")
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--staging-bucket")
    parser.add_argument("--staging-region")
    parser.add_argument("--staging-api-role-arn")
    parser.add_argument("--staging-worker-role-arn")
    parser.add_argument("--staging-kms-key")
    args = parser.parse_args()
    rendered = render_manifest(
        args.input.read_text(),
        args.image,
        staging_bucket=args.staging_bucket,
        staging_region=args.staging_region,
        staging_api_role_arn=args.staging_api_role_arn,
        staging_worker_role_arn=args.staging_worker_role_arn,
        staging_kms_key=args.staging_kms_key,
    )
    args.output.write_text(rendered)


if __name__ == "__main__":
    main()
