import os

import pytest

from docling_serve.upload_staging import check_upload_staging_capability


@pytest.mark.skipif(
    os.getenv("DOCLING_SERVE_RUN_LIVE_S3_STAGING") != "1",
    reason="Set DOCLING_SERVE_RUN_LIVE_S3_STAGING=1 in the IAM-enabled image job",
)
def test_live_s3_staging_lifecycle_encryption_and_canary():
    """Opt-in production check using only the pod/runner's ambient IAM role."""

    check_upload_staging_capability(force=True)
