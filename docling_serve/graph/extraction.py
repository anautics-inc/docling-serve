"""Knowledge-graph extraction (the NER replacement): template-driven entity +
relationship extraction over already-converted document text.

This is a thin, self-contained wrapper around ``docling-graph`` that operates on
the text docling already produced (native conversion → markdown). It does NOT
re-implement conversion — that is docling's out-of-the-box job. The caller hands
us converted text; we run docling-graph through the LiteLLM proxy and return a
JSON node/edge payload.

Graceful degradation: when docling-graph is not installed, the LiteLLM endpoint
is unconfigured, or a run fails, we raise :class:`GraphExtractionUnavailable`,
which the endpoint maps to an empty graph + a ``note`` so callers degrade
uniformly instead of erroring.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docling_serve.identity import current_identity
from docling_serve.settings import docling_serve_settings

_log = logging.getLogger(__name__)

_DEFAULT_TEMPLATE = "docling_serve.graph.templates.DocumentGraph"


class GraphExtractionUnavailable(RuntimeError):
    """Raised when docling-graph cannot run (missing dep, bad config, LLM error)."""


@dataclass(slots=True)
class _GraphConfig:
    base_url: str
    api_key: str
    model: str
    provider: str
    template: str
    contract: str
    structured_output: bool
    max_chars: int
    max_output_tokens: int
    context_limit: int


def docling_graph_installed() -> bool:
    """True when ``docling-graph`` is importable (a declared dep; defensive guard)."""
    return importlib.util.find_spec("docling_graph") is not None


def build_graph_config(template_override: str | None = None) -> _GraphConfig | None:
    """Resolve graph-extraction config from settings, or ``None`` when unconfigured.

    ``template_override`` (a dotted import path) wins over the configured default,
    letting a caller select a domain template per request.
    """
    base_url = getattr(docling_serve_settings, "graph_litellm_base_url", None) or getattr(
        docling_serve_settings, "litellm_base_url", None
    )
    api_key = getattr(docling_serve_settings, "graph_litellm_api_key", None) or getattr(
        docling_serve_settings, "litellm_api_key", None
    )
    if not base_url or not api_key:
        return None
    template = (
        template_override
        or getattr(docling_serve_settings, "graph_extraction_template", None)
        or _DEFAULT_TEMPLATE
    )
    return _GraphConfig(
        base_url=base_url,
        api_key=api_key,
        model=getattr(docling_serve_settings, "graph_litellm_model", "bedrock-claude-sonnet-4-6"),
        provider=getattr(docling_serve_settings, "graph_litellm_provider", "litellm_proxy"),
        template=template,
        contract=getattr(docling_serve_settings, "graph_extraction_contract", "direct"),
        structured_output=bool(
            getattr(docling_serve_settings, "graph_extraction_structured_output", False)
        ),
        max_chars=int(getattr(docling_serve_settings, "graph_extraction_max_chars", 200_000)),
        max_output_tokens=int(
            getattr(docling_serve_settings, "graph_extraction_max_output_tokens", 32_000)
        ),
        context_limit=int(
            getattr(docling_serve_settings, "graph_extraction_context_limit", 200_000)
        ),
    )


def _allowed_templates() -> set[str]:
    """Dotted template paths the service will import.

    Limited to the built-in default, the per-profile domain templates, and the
    server-configured override. A request must not name an arbitrary importable
    module: ``importlib.import_module`` runs the target's top-level code, so an
    unrestricted request string is a code-execution gadget.
    """
    from docling_serve.graph.templates import PROFILE_TEMPLATES

    allowed = set(PROFILE_TEMPLATES.values())
    allowed.add(_DEFAULT_TEMPLATE)
    configured = getattr(docling_serve_settings, "graph_extraction_template", None)
    if configured:
        allowed.add(configured)
    return allowed


def _import_template(dotted: str) -> Any:
    if dotted not in _allowed_templates():
        raise GraphExtractionUnavailable(f"template_not_allowed: {dotted}")
    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        raise GraphExtractionUnavailable(f"bad_template_path: {dotted}")
    try:
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    except (ImportError, AttributeError) as err:
        raise GraphExtractionUnavailable(f"template_not_found: {dotted}") from err


def run_graph_extraction(source_path: Path, cfg: _GraphConfig) -> tuple[Any, int]:
    """Run docling-graph through LiteLLM; return ``(networkx graph, model count)``.

    docling-graph is imported lazily to keep start-up light and to fail soft if
    the (declared) package is somehow unavailable at runtime.
    """
    try:
        dg = importlib.import_module("docling_graph")
        dg_cfg = importlib.import_module("docling_graph.llm_clients.config")
        from pydantic import SecretStr
    except Exception as err:  # pragma: no cover - import guard
        raise GraphExtractionUnavailable("docling_graph_import_failed") from err

    template_cls = _import_template(cfg.template)

    # Spend attribution: the proxy key is service-scoped, so the caller's identity
    # headers (recorded as spend-log tags by the proxy) isolate usage per user/tenant.
    request_headers = {"x-litellm-tags": "docling-graph"}
    identity = current_identity()
    if identity is not None:
        request_headers.update(identity.headers())

    pipeline_config = dg.PipelineConfig(
        source=str(source_path),
        template=template_cls,
        backend="llm",
        inference="remote",
        provider_override=cfg.provider,
        model_override=cfg.model,
        extraction_contract=cfg.contract,
        processing_mode="many-to-one",
        structured_output=cfg.structured_output,
        use_chunking=False,
        gleaning_enabled=False,
        dump_to_disk=False,
        llm_overrides=dg_cfg.LlmRuntimeOverrides(
            connection=dg_cfg.ConnectionOverrides(
                base_url=cfg.base_url,
                api_key=SecretStr(cfg.api_key),
                headers=request_headers,
            ),
            # docling-graph can't resolve token limits through a proxy alias and falls
            # back to 4092 output tokens, truncating document-scale extractions mid-JSON.
            # Pin the real model budgets explicitly.
            generation=dg_cfg.GenerationOverrides(max_tokens=cfg.max_output_tokens),
            max_output_tokens=cfg.max_output_tokens,
            context_limit=cfg.context_limit,
        ),
    )

    try:
        ctx = dg.run_pipeline(pipeline_config.to_dict())
    except Exception as err:
        raise GraphExtractionUnavailable(f"graph_run_failed: {type(err).__name__}") from err

    graph = getattr(ctx, "knowledge_graph", None)
    if graph is None:
        raise GraphExtractionUnavailable("no_graph_returned")
    model_count = len(getattr(ctx, "extracted_models", []) or [])
    return graph, model_count


def _graph_to_payload(graph: Any) -> dict[str, Any]:
    """Serialize a NetworkX-style DiGraph into a JSON-friendly node/edge payload.

    Duck-typed against the NetworkX API (``nodes(data=True)`` / ``edges(data=True)``)
    so it needs no networkx import and is trivially testable with a fake graph.
    """
    nodes: list[dict[str, Any]] = []
    labels: dict[str, int] = {}
    for node_id, attrs in graph.nodes(data=True):
        attrs = dict(attrs or {})
        label = attrs.get("label")
        if label:
            labels[label] = labels.get(label, 0) + 1
        properties = {
            k: v
            for k, v in attrs.items()
            if k not in {"id", "label", "type", "__class__"} and v not in (None, "")
        }
        nodes.append(
            {
                "id": str(node_id),
                "label": label,
                "type": attrs.get("type"),
                "properties": properties,
            }
        )

    edges: list[dict[str, Any]] = []
    edge_labels: dict[str, int] = {}
    for source, target, attrs in graph.edges(data=True):
        attrs = dict(attrs or {})
        edge_label = attrs.get("label")
        if edge_label:
            edge_labels[edge_label] = edge_labels.get(edge_label, 0) + 1
        properties = {k: v for k, v in attrs.items() if k != "label" and v not in (None, "")}
        edges.append(
            {
                "source": str(source),
                "target": str(target),
                "label": edge_label,
                "properties": properties,
            }
        )

    return {"nodes": nodes, "edges": edges, "labels": labels, "edgeLabels": edge_labels}


def graph_payload_from_text(text: str, *, template: str | None = None) -> dict[str, Any]:
    """Extract a knowledge graph from already-converted markdown/text.

    Returns the node/edge payload plus summary metadata. Raises
    :class:`GraphExtractionUnavailable` when docling-graph is unavailable, the
    LiteLLM endpoint is unconfigured, or the run fails.
    """
    if not docling_graph_installed():
        raise GraphExtractionUnavailable("docling_graph_not_installed")
    cfg = build_graph_config(template_override=template)
    if cfg is None:
        raise GraphExtractionUnavailable("litellm_not_configured")
    if not text or not text.strip():
        raise GraphExtractionUnavailable("no_source_text")

    truncated = text[: cfg.max_chars]
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(truncated)
            tmp_path = Path(handle.name)
        graph, model_count = run_graph_extraction(tmp_path, cfg)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    payload = _graph_to_payload(graph)
    return {
        "template": cfg.template,
        "model": {"provider": cfg.provider, "modelId": cfg.model},
        "extractedModels": model_count,
        "nodeCount": len(payload["nodes"]),
        "edgeCount": len(payload["edges"]),
        **payload,
    }
