#!/usr/bin/env python3
"""Backfill Content-Type on already-published bundle artifacts in S3.

Objects uploaded before typed publishing landed carry ``binary/octet-stream``;
same-origin proxies serve them with ``nosniff``, so browsers refuse SVG/PNG
artifacts in image contexts. This walks a prefix, and for every object whose
stored type is generic AND whose extension maps to a known artifact type,
issues an in-place ``CopyObject`` with the corrected Content-Type (metadata
REPLACE — bytes untouched, idempotent, safe to re-run or interrupt).

Usage:
    python scripts/backfill_bundle_content_types.py \
        --bucket captify-core \
        --prefix tenants/anautics/document-extractions/ \
        [--apply]          # default is a dry run report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docling_serve.storage import content_type_for  # noqa: E402

GENERIC_TYPES = {"binary/octet-stream", "application/octet-stream", ""}
DEFAULT_TYPE = "application/octet-stream"


def backfill(bucket: str, prefix: str, apply: bool) -> tuple[int, int, int]:
    """Returns (scanned, retyped, skipped)."""
    import boto3

    client = boto3.client("s3")
    paginator = client.get_paginator("list_objects_v2")
    scanned = retyped = skipped = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []):
            key = entry["Key"]
            scanned += 1
            desired = content_type_for(key)
            if desired == DEFAULT_TYPE:
                skipped += 1  # no better type known — leave untouched
                continue
            head = client.head_object(Bucket=bucket, Key=key)
            current = (head.get("ContentType") or "").split(";")[0].strip().lower()
            if current not in GENERIC_TYPES:
                skipped += 1  # already carries a real type — never overwrite
                continue
            print(f"{'RETYPE' if apply else 'DRY-RUN'} {key}: {current or '(none)'} -> {desired}")
            if apply:
                client.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key},
                    ContentType=desired,
                    Metadata=head.get("Metadata") or {},
                    MetadataDirective="REPLACE",
                )
            retyped += 1
    return scanned, retyped, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    scanned, retyped, skipped = backfill(args.bucket, args.prefix, args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"\n{mode}: scanned={scanned} retyped={retyped} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
