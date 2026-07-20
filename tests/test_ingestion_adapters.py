from __future__ import annotations

from types import SimpleNamespace

import pytest

from docling_serve.capabilities import CAPABILITIES
from docling_serve.ingestion.adapters import ADAPTERS, execute_adapter
from docling_serve.ingestion.adapters.registry import adapter_readiness


def test_every_capability_has_one_executable_adapter() -> None:
    assert set(ADAPTERS) == set(CAPABILITIES)
    for domain, adapter in ADAPTERS.items():
        assert adapter.domain == domain
        assert adapter.handler_name == (CAPABILITIES[domain].runtime_adapter or domain)


def test_adapter_owns_admission_limit() -> None:
    settings = SimpleNamespace(
        max_file_size=100,
        legacy_office_max_input_bytes=40,
    )
    assert ADAPTERS["document"].admission_limit(settings) == 100
    assert ADAPTERS["legacy-office"].admission_limit(settings) == 40


@pytest.mark.asyncio
async def test_registry_dispatches_without_domain_branching() -> None:
    calls: list[str] = []

    async def handler() -> dict[str, str]:
        calls.append("called")
        return {"domain": "access"}

    result = await execute_adapter("access", {"access": handler})
    assert result == {"domain": "access"}
    assert calls == ["called"]


def test_graph_readiness_is_additive() -> None:
    readiness = adapter_readiness()
    assert isinstance(readiness["graph-extraction"], bool)
    assert readiness["document"] is True
