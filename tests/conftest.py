"""Test-suite gates for live services and heavyweight local models."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_LIVE_SERVER_PREFIXES = ("test_1-", "test_2-")
_MODEL_TESTS = {
    "tests/test_file_opts.py::test_convert_file",
    "tests/test_fastapi_endpoints.py::test_convert_file",
    "tests/test_fastapi_endpoints.py::test_referenced_artifacts",
    "tests/test_results_clear.py::test_clear_results",
    "tests/test_results_clear.py::test_delay_remove",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    run_live = os.getenv("DOCLING_SERVE_RUN_LIVE_TESTS") == "1"
    run_models = os.getenv("DOCLING_SERVE_RUN_MODEL_TESTS") == "1"
    for item in items:
        filename = Path(str(item.fspath)).name
        if not run_live and filename.startswith(_LIVE_SERVER_PREFIXES):
            item.add_marker(
                pytest.mark.skip(
                    reason="requires a live Docling Serve endpoint; "
                    "set DOCLING_SERVE_RUN_LIVE_TESTS=1"
                )
            )
        if not run_models and any(
            item.nodeid.startswith(nodeid) for nodeid in _MODEL_TESTS
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason="requires heavyweight local model execution; "
                    "set DOCLING_SERVE_RUN_MODEL_TESTS=1"
                )
            )
