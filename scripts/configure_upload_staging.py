"""Provision or verify the fixed upload-staging lifecycle rule using ambient IAM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from docling_serve.upload_staging import (
    STAGING_CLAIM_LIFECYCLE_RULE_ID,
    STAGING_CLEANUP_CLAIM_PREFIX,
    STAGING_CLEANUP_DEAD_PREFIX,
    STAGING_CLEANUP_LIFECYCLE_RULE_ID,
    STAGING_CLEANUP_QUEUE_PREFIX,
    STAGING_DEAD_LIFECYCLE_RULE_ID,
    STAGING_LIFECYCLE_RULE_ID,
    STAGING_TAG_KEY,
    STAGING_TAG_VALUE,
)

FIXED_PREFIX = "docling-staging/v1/"


def _expiration_rule(rule_id: str, prefix: str, days: int) -> dict[str, Any]:
    return {
        "ID": rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": prefix},
        "Expiration": {"Days": days},
        "NoncurrentVersionExpiration": {"NoncurrentDays": days},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": days},
    }


def lifecycle_configuration(
    retention_days: int,
    *,
    cleanup_retention_days: int = 7,
    dead_letter_retention_days: int = 30,
    claim_retention_days: int = 1,
) -> dict[str, Any]:
    if not 1 <= retention_days <= 7:
        raise ValueError("retention_days must be between 1 and 7")
    if not 1 <= cleanup_retention_days <= 30:
        raise ValueError("cleanup_retention_days must be between 1 and 30")
    if not 1 <= dead_letter_retention_days <= 90:
        raise ValueError("dead_letter_retention_days must be between 1 and 90")
    if not 1 <= claim_retention_days <= 7:
        raise ValueError("claim_retention_days must be between 1 and 7")
    return {
        "Rules": [
            {
                "ID": STAGING_LIFECYCLE_RULE_ID,
                "Status": "Enabled",
                "Filter": {
                    "And": {
                        "Prefix": FIXED_PREFIX,
                        "Tags": [{"Key": STAGING_TAG_KEY, "Value": STAGING_TAG_VALUE}],
                    }
                },
                "Expiration": {"Days": retention_days},
                "NoncurrentVersionExpiration": {
                    "NoncurrentDays": retention_days,
                },
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": retention_days,
                },
            },
            _expiration_rule(
                STAGING_CLEANUP_LIFECYCLE_RULE_ID,
                STAGING_CLEANUP_QUEUE_PREFIX,
                cleanup_retention_days,
            ),
            _expiration_rule(
                STAGING_DEAD_LIFECYCLE_RULE_ID,
                STAGING_CLEANUP_DEAD_PREFIX,
                dead_letter_retention_days,
            ),
            _expiration_rule(
                STAGING_CLAIM_LIFECYCLE_RULE_ID,
                STAGING_CLEANUP_CLAIM_PREFIX,
                claim_retention_days,
            ),
        ]
    }


def _merge_lifecycle_rules(
    existing_rules: list[dict[str, Any]],
    expected_rules: list[dict[str, Any]],
    *,
    allow_safe_migration: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_by_id = {rule["ID"]: rule for rule in expected_rules}
    managed_ids = set(expected_by_id)
    stable_counts = {
        rule_id: sum(rule.get("ID") == rule_id for rule in existing_rules)
        for rule_id in managed_ids
    }
    conflicts = [
        rule
        for rule in existing_rules
        if rule.get("ID") not in managed_ids and _is_conflicting_staging_rule(rule)
    ]
    if any(count > 1 for count in stable_counts.values()) or (
        conflicts and not allow_safe_migration
    ):
        raise RuntimeError(
            "conflicting staging lifecycle rules require --allow-safe-migration"
        )

    merged_rules: list[dict[str, Any]] = []
    replaced: set[str] = set()
    for rule in existing_rules:
        if rule in conflicts:
            continue
        rule_id = rule.get("ID")
        if isinstance(rule_id, str) and rule_id in managed_ids:
            if rule_id not in replaced:
                merged_rules.append(expected_by_id[rule_id])
                replaced.add(rule_id)
            continue
        merged_rules.append(rule)
    merged_rules.extend(rule for rule in expected_rules if rule["ID"] not in replaced)
    unrelated = [
        rule
        for rule in existing_rules
        if rule.get("ID") not in managed_ids and rule not in conflicts
    ]
    return merged_rules, unrelated


def configure(
    client: Any,
    *,
    bucket: str,
    retention_days: int,
    check_only: bool,
    cleanup_retention_days: int = 7,
    dead_letter_retention_days: int = 30,
    claim_retention_days: int = 1,
    allow_safe_migration: bool = False,
    expected_bucket_policy: dict[str, Any] | None = None,
) -> None:
    expected_rules = lifecycle_configuration(
        retention_days,
        cleanup_retention_days=cleanup_retention_days,
        dead_letter_retention_days=dead_letter_retention_days,
        claim_retention_days=claim_retention_days,
    )["Rules"]
    expected_by_id = {rule["ID"]: rule for rule in expected_rules}
    managed_ids = set(expected_by_id)
    try:
        before = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as exc:
        error_code = (
            getattr(exc, "response", {}).get("Error", {}).get("Code")
            if hasattr(exc, "response")
            else type(exc).__name__
        )
        if error_code != "NoSuchLifecycleConfiguration":
            raise
        before = {"Rules": []}
    existing_rules = list(before.get("Rules", []))
    merged_rules, unrelated_before = _merge_lifecycle_rules(
        existing_rules,
        expected_rules,
        allow_safe_migration=allow_safe_migration,
    )

    if not check_only:
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={"Rules": merged_rules},
        )
    actual = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    for rule_id, expected_rule in expected_by_id.items():
        matches = [
            rule for rule in actual.get("Rules", []) if rule.get("ID") == rule_id
        ]
        if len(matches) != 1 or matches[0] != expected_rule:
            raise RuntimeError("staging lifecycle rules do not exactly match policy")
    unrelated_after = [
        rule for rule in actual.get("Rules", []) if rule.get("ID") not in managed_ids
    ]
    if unrelated_after != unrelated_before:
        raise RuntimeError("unrelated lifecycle rules changed during staging upsert")
    if expected_bucket_policy is not None:
        policy = json.loads(client.get_bucket_policy(Bucket=bucket)["Policy"])
        if policy != expected_bucket_policy:
            raise RuntimeError("dedicated staging bucket policy does not match")


def _is_conflicting_staging_rule(rule: dict[str, Any]) -> bool:
    rule_id = str(rule.get("ID", "")).lower()
    if "docling-staging" in rule_id:
        return True
    filter_value = rule.get("Filter")
    if not isinstance(filter_value, dict):
        return False
    conjunction = filter_value.get("And")
    if isinstance(conjunction, dict):
        prefix = conjunction.get("Prefix")
        tags = conjunction.get("Tags", [])
        has_tag = any(
            isinstance(tag, dict)
            and tag.get("Key") == STAGING_TAG_KEY
            and tag.get("Value") == STAGING_TAG_VALUE
            for tag in tags
        )
        return prefix == FIXED_PREFIX or has_tag
    return filter_value.get("Prefix") in {
        FIXED_PREFIX,
        STAGING_CLEANUP_QUEUE_PREFIX,
        STAGING_CLEANUP_DEAD_PREFIX,
        STAGING_CLEANUP_CLAIM_PREFIX,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--retention-days", type=int, default=1)
    parser.add_argument("--cleanup-retention-days", type=int, default=7)
    parser.add_argument("--dead-letter-retention-days", type=int, default=30)
    parser.add_argument("--claim-retention-days", type=int, default=1)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-safe-migration", action="store_true")
    parser.add_argument("--bucket-policy", type=Path)
    args = parser.parse_args()

    import boto3

    client = boto3.client("s3", region_name=args.region)
    configure(
        client,
        bucket=args.bucket,
        retention_days=args.retention_days,
        cleanup_retention_days=args.cleanup_retention_days,
        dead_letter_retention_days=args.dead_letter_retention_days,
        claim_retention_days=args.claim_retention_days,
        check_only=args.check_only,
        allow_safe_migration=args.allow_safe_migration,
        expected_bucket_policy=(
            json.loads(args.bucket_policy.read_text()) if args.bucket_policy else None
        ),
    )


if __name__ == "__main__":
    main()
