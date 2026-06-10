"""Knowledge-graph extraction: template-driven entity+relation extraction.

This is the AWS Comprehend NER replacement. docling-graph uses a Pydantic
*template* to extract a schema-validated, directed knowledge graph (typed
entities + relationships) from text via an LLM. The LLM call is routed through
the existing LiteLLM proxy (which fronts Bedrock), so no model SDK is embedded
here.

Two consumers share one core:

* :class:`GraphExtractionEnhancer` — the opt-in ``knowledge_graph`` enrichment
  pass that runs over a deep-extraction bundle's ``document.md`` and writes a
  ``knowledge-graph.json`` sidecar.
* :func:`graph_payload_from_text` — a stateless entry point (used by the
  ``/v1/graph/extract`` endpoint) that extracts a graph from raw markdown/text
  without a bundle, so the gateway can replace its NER pass with one HTTP call.

docling-graph is a declared dependency (it ships with the service). The
lazy-import + graceful degradation below is defensive: when the LiteLLM endpoint
is not configured the enhancer records a note and the endpoint returns an empty
graph instead of failing.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docling_serve.deep_document.artifact_writer import write_json
from docling_serve.extractors.base import ExtractionContext, ExtractorResult
from docling_serve.extractors.enhancers.base import EnhancementResult, Enhancer
from docling_serve.settings import docling_serve_settings

_log = logging.getLogger(__name__)

_DEFAULT_TEMPLATE = "docling_serve.extractors.enhancers.graph_templates.DocumentGraph"


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


# --------------------------------------------------------------------------- #
# Reusable core (shared by the enhancer and the /v1/graph/extract endpoint)   #
# --------------------------------------------------------------------------- #


def docling_graph_installed() -> bool:
    """True when ``docling-graph`` is importable (a declared dep; this is a defensive guard)."""
    return importlib.util.find_spec("docling_graph") is not None


def build_graph_config(template_override: str | None = None) -> _GraphConfig | None:
    """Resolve graph-extraction config from settings, or ``None`` when unconfigured.

    ``template_override`` (a dotted import path) wins over the configured default,
    letting a caller select a domain template per request.
    """
    base_url = getattr(docling_serve_settings, "graph_litellm_base_url", None)
    api_key = getattr(docling_serve_settings, "graph_litellm_api_key", None)
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


def run_graph_extraction(source_path: Path, cfg: _GraphConfig) -> tuple[Any, int]:
    """Run docling-graph through LiteLLM; return ``(networkx graph, model count)``.

    Imports docling-graph lazily to keep service start-up light and to fail soft
    if the (declared) package is somehow unavailable at runtime.
    """
    try:
        dg = importlib.import_module("docling_graph")
        dg_cfg = importlib.import_module("docling_graph.llm_clients.config")
        from pydantic import SecretStr
    except Exception as err:  # pragma: no cover - import guard
        raise GraphExtractionUnavailable("docling_graph_import_failed") from err

    template_cls = _import_template(cfg.template)

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
                # Tag for LiteLLM spend attribution: the proxy key is shared
                # platform-wide, so tags are the only way to isolate graph-extraction
                # token usage/cost in spend logs and dashboards.
                headers={"x-litellm-tags": "docling-graph"},
            ),
            # docling-graph cannot resolve token limits through a proxy alias and
            # falls back to 4092 output tokens, which truncates document-scale
            # extractions mid-JSON. Pin the real model budgets explicitly.
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


def graph_payload_from_text(text: str, *, template: str | None = None) -> dict[str, Any]:
    """Extract a knowledge graph from raw markdown/text (stateless entry point).

    Returns the node/edge payload plus summary metadata. Raises
    :class:`GraphExtractionUnavailable` when docling-graph is not installed, the
    LiteLLM endpoint is not configured, or the run fails — callers map that to a
    graceful empty response.
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


# --------------------------------------------------------------------------- #
# Bundle enrichment pass                                                      #
# --------------------------------------------------------------------------- #


class GraphExtractionEnhancer(Enhancer):
    name = "knowledge_graph"
    aliases = ("graph", "kg", "knowledge-graph", "graph_extraction", "entities")

    def applies(self, ctx: ExtractionContext, document: dict[str, Any]) -> bool:
        if build_graph_config() is None:
            return False
        if not docling_graph_installed():
            return False
        return self._source_text(ctx, document) is not None

    def enhance(
        self,
        ctx: ExtractionContext,
        document: dict[str, Any],
        *,
        base_result: ExtractorResult,
    ) -> EnhancementResult:
        result = EnhancementResult(name=self.name)

        # Profile-aware template: a schematic/access extraction profile selects the
        # matching domain template (more specific than the global setting); unknown
        # profiles fall back to the configured/global default template.
        from docling_serve.extractors.enhancers.graph_templates import (
            resolve_profile_template,
        )

        cfg = build_graph_config(
            template_override=resolve_profile_template(getattr(ctx, "profile", None))
        )
        if cfg is None:
            result.notes.append("litellm_not_configured")
            return result
        if not docling_graph_installed():
            result.notes.append("docling_graph_not_installed")
            return result

        source_path = self._source_text(ctx, document)
        if source_path is None:
            result.notes.append("no_source_text")
            return result

        try:
            graph, model_count = run_graph_extraction(source_path, cfg)
        except GraphExtractionUnavailable as err:
            _log.warning("Knowledge-graph extraction unavailable: %s", err)
            result.notes.append(str(err))
            return result

        payload = _graph_to_payload(graph)
        if not payload["nodes"]:
            result.notes.append("empty_graph")
            return result

        summary = {
            "template": cfg.template,
            "model": {"provider": cfg.provider, "modelId": cfg.model},
            "extractedModels": model_count,
            "nodeCount": len(payload["nodes"]),
            "edgeCount": len(payload["edges"]),
            "labels": payload["labels"],
            "edgeLabels": payload["edgeLabels"],
        }

        sidecar = ctx.bundle_dir / "knowledge-graph.json"
        write_json(sidecar, {**summary, **payload})
        rel = sidecar.relative_to(ctx.bundle_dir).as_posix()

        document["knowledgeGraph"] = {**summary, "sidecar": rel}

        result.applied = True
        result.artifacts.append(rel)
        result.manifest_extra = {"knowledgeGraph": {**summary, "sidecar": rel}}
        return result

    # -- internals -------------------------------------------------------

    def _source_text(self, ctx: ExtractionContext, document: dict[str, Any]) -> Path | None:
        """Return a markdown path to feed docling-graph, or None when absent.

        Prefers the bundle's ``document.md`` (already produced). Falls back to
        writing the deep document's plain text to a sidecar. Truncates to the
        configured character ceiling.
        """
        cfg = build_graph_config()
        max_chars = cfg.max_chars if cfg else 200_000

        md = ctx.bundle_dir / "document.md"
        text: str | None = None
        if md.is_file():
            text = md.read_text(encoding="utf-8", errors="ignore")
        else:
            text = _document_plain_text(document)

        if not text or not text.strip():
            return None

        if md.is_file() and len(text) <= max_chars:
            return md

        truncated = text[:max_chars]
        graph_src = ctx.bundle_dir / ".graph-source.md"
        graph_src.write_text(truncated, encoding="utf-8")
        return graph_src


def _allowed_templates() -> set[str]:
    """Dotted template paths the service will import.

    Limited to the built-in default, the per-profile domain templates, and the
    server-configured override. A request must not be able to name an arbitrary
    importable module: ``importlib.import_module`` runs the target module's
    top-level code, so an unrestricted request string is a code-execution gadget.
    """
    from docling_serve.extractors.enhancers.graph_templates import PROFILE_TEMPLATES

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


def _graph_to_payload(graph: Any) -> dict[str, Any]:
    """Serialize a NetworkX-style DiGraph into a JSON-friendly node/edge payload.

    Duck-typed against the NetworkX API (``nodes(data=True)`` /
    ``edges(data=True)``) so it needs no networkx import and is trivially
    testable with a lightweight fake graph.
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

    return {
        "nodes": nodes,
        "edges": edges,
        "labels": labels,
        "edgeLabels": edge_labels,
    }


def _document_plain_text(document: dict[str, Any]) -> str | None:
    parts: list[str] = []
    units = (document.get("document") or {}).get("units") or []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        for element in (unit.get("content") or {}).get("elements") or []:
            if isinstance(element, dict):
                text = element.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
    return "\n".join(parts) if parts else None
