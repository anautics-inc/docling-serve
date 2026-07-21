from __future__ import annotations

import pytest

from scripts.verify_production_samples import (
    DEFAULT_THRESHOLDS,
    validate_summaries,
)


def _summaries() -> list[dict]:
    return [
        {
            "entryCount": 10,
            "figureCount": 2,
            "renderedFigures": 2,
            "schematicComponents": 0,
            "schematicNets": 0,
        },
        {
            "entryCount": 5,
            "figureCount": 1,
            "renderedFigures": 1,
            "schematicComponents": 4,
            "schematicNets": 3,
        },
        {
            "entryCount": 1,
            "figureCount": 1,
            "renderedFigures": 1,
            "schematicComponents": 0,
            "schematicNets": 0,
        },
    ]


def test_production_sample_thresholds_accept_complete_evidence() -> None:
    validate_summaries(_summaries(), DEFAULT_THRESHOLDS)


def test_production_sample_thresholds_reject_regression() -> None:
    summaries = _summaries()
    summaries[1]["schematicComponents"] = 0
    summaries[1]["schematicNets"] = 0

    with pytest.raises(RuntimeError, match="Production sample regression"):
        validate_summaries(summaries, DEFAULT_THRESHOLDS)
