"""Offline tests for the graph-extraction module (Article IV / VIII.3).

No live LLM, no network: these cover the template allow-list (code-execution
guard), the degraded-path raises in ``graph_payload_from_text``, the
``_graph_to_payload`` shape against a fake DiGraph, and the unconfigured config
resolution.
"""

import pytest
from pydantic import SecretStr

from docling_serve.graph import extraction
from docling_serve.graph.extraction import (
    GraphExtractionUnavailable,
    _GraphConfig,
    _import_template,
    build_graph_config,
    graph_payload_from_text,
)
from docling_serve.graph.templates import DocumentGraph


def _dummy_config() -> _GraphConfig:
    return _GraphConfig(
        base_url="http://proxy.invalid",
        api_key=SecretStr("dummy"),
        model="bedrock-claude",
        provider="litellm_proxy",
        template="docling_serve.graph.templates.DocumentGraph",
        contract="direct",
        structured_output=False,
        max_chars=1000,
        max_output_tokens=1000,
        context_limit=1000,
        timeout_s=1.0,
    )


def test_disallowed_template_rejected():
    with pytest.raises(GraphExtractionUnavailable):
        _import_template("os.system")


def test_allowed_template_imports():
    assert (
        _import_template("docling_serve.graph.templates.DocumentGraph") is DocumentGraph
    )


def test_payload_empty_text_raises(monkeypatch):
    # Isolate from install/config checks so we reach the empty-text guard.
    monkeypatch.setattr(extraction, "docling_graph_installed", lambda: True)
    monkeypatch.setattr(
        extraction, "build_graph_config", lambda template_override=None: _dummy_config()
    )

    with pytest.raises(GraphExtractionUnavailable) as exc:
        graph_payload_from_text("  ")
    assert "no_source_text" in str(exc.value)


def test_graph_to_payload_shape():
    class _FakeGraph:
        def nodes(self, data=False):
            # A list-valued attribute must not blow up _is_present (F19).
            return [
                ("n1", {"label": "Person", "type": "Entity", "aliases": ["a", "b"]})
            ]

        def edges(self, data=False):
            return [("n1", "n2", {"label": "KNOWS", "weight": 0.5})]

    payload = extraction._graph_to_payload(_FakeGraph())

    assert set(payload.keys()) == {"nodes", "edges", "labels", "edgeLabels"}
    assert payload["nodes"][0]["properties"]["aliases"] == ["a", "b"]
    assert payload["labels"] == {"Person": 1}
    assert payload["edgeLabels"] == {"KNOWS": 1}


def test_unconfigured_returns_none(monkeypatch):
    for attr in (
        "graph_litellm_base_url",
        "litellm_base_url",
        "graph_litellm_api_key",
        "litellm_api_key",
    ):
        monkeypatch.setattr(extraction.docling_serve_settings, attr, None)

    assert build_graph_config() is None
