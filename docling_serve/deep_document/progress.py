"""Live progress for deep-extraction bundles.

Deep extraction can take minutes (schematics run two vision passes plus
geometric tracing), and the bundle only lands in S3 at the end — so until now
a client saw nothing but a spinner. :class:`S3ProgressReporter` gives the
pipeline a side channel: each reported stage is appended to a small
``progress.json`` object written directly at the bundle's S3 prefix, which
clients (the workbench's converting surface) poll through the same bundle
proxy they later read the artifacts from.

Best-effort by design: every write failure is swallowed — progress must never
fail or slow the extraction itself.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger(__name__)

#: Bundle-relative object name (also special-cased for short caching by readers).
PROGRESS_OBJECT = "progress.json"


class S3ProgressReporter:
    """Append-only stage log published to ``s3://{bucket}/{prefix}/progress.json``.

    Callable with the :meth:`ExtractionContext.report_progress` signature:
    ``reporter(stage, detail)``.
    """

    def __init__(self, *, bucket: str, prefix: str, task_id: str) -> None:
        import boto3

        self._bucket = bucket
        self._key = f"{prefix.strip('/')}/{PROGRESS_OBJECT}"
        self._task_id = task_id
        self._client = boto3.client("s3")
        self._stages: list[dict[str, Any]] = []
        self._done = False

    def __call__(self, stage: str, detail: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {
            "stage": str(stage),
            "at": datetime.now(UTC).isoformat(),
        }
        if detail:
            entry["detail"] = detail
        self._stages.append(entry)
        self._publish()

    def complete(self) -> None:
        """Mark the run finished (clients stop polling on ``done``)."""
        self._done = True
        self._publish()

    def _publish(self) -> None:
        body = json.dumps(
            {
                "taskId": self._task_id,
                "updatedAt": datetime.now(UTC).isoformat(),
                "done": self._done,
                "stages": self._stages,
            }
        )
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as error:
            _log.debug("Progress publish to s3://%s/%s failed: %s", self._bucket, self._key, error)
