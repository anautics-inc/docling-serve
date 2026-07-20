"""Installed runtime-only S3 staging canary for production image CI."""

from __future__ import annotations

from docling_serve.settings import docling_serve_settings
from docling_serve.upload_staging import check_upload_staging_capability


def main() -> None:
    if docling_serve_settings.upload_staging_mode != "required":
        raise RuntimeError("staging runtime smoke requires mode=required")
    check_upload_staging_capability(force=True)
    print("IAM staging lifecycle/encryption/canary check passed")


if __name__ == "__main__":
    main()
