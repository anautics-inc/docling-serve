"""Enhancement registry and dispatch.

Enhancers are opt-in passes that enrich an extractor's default output. Resolve
requested names (and aliases) against the registry and run those that apply:

    run_enhancements(ctx, document, base_result) -> list[EnhancementResult]

Add a new enrichment by registering one :class:`Enhancer`; extractors stay
untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from docling_serve.extractors.base import ExtractionContext, ExtractorResult
from docling_serve.extractors.enhancers.base import EnhancementResult, Enhancer
from docling_serve.extractors.enhancers.graph_extraction import (
    GraphExtractionEnhancer,
    GraphExtractionUnavailable,
    docling_graph_installed,
    graph_payload_from_text,
)
from docling_serve.extractors.enhancers.graph_templates import resolve_profile_template
from docling_serve.extractors.enhancers.image_context import (
    ImageContextEnhancer,
    ImageContextUnavailable,
    describe_file_images,
    extract_file_images,
)

_log = logging.getLogger(__name__)

_REGISTRY: list[Enhancer] = [
    ImageContextEnhancer(),
    GraphExtractionEnhancer(),
]


def _resolve(name: str) -> Enhancer | None:
    key = name.strip().lower().replace("-", "_")
    for enhancer in _REGISTRY:
        names = {enhancer.name, *enhancer.aliases}
        if key in {n.lower().replace("-", "_") for n in names}:
            return enhancer
    return None


def available_enhancers() -> list[str]:
    return [enhancer.name for enhancer in _REGISTRY]


def run_enhancements(
    ctx: ExtractionContext,
    document: dict[str, Any],
    *,
    base_result: ExtractorResult,
) -> list[EnhancementResult]:
    """Run each requested, applicable enhancer; mutate ``document`` in place."""
    results: list[EnhancementResult] = []
    seen: set[str] = set()
    for requested in ctx.enhancements or []:
        enhancer = _resolve(requested)
        if enhancer is None:
            _log.warning("Unknown enhancement requested: %s", requested)
            continue
        if enhancer.name in seen:
            continue
        seen.add(enhancer.name)
        try:
            if not enhancer.applies(ctx, document):
                continue
            results.append(enhancer.enhance(ctx, document, base_result=base_result))
        except Exception:
            _log.exception("Enhancer %s failed", enhancer.name)
            results.append(
                EnhancementResult(name=enhancer.name, notes=["enhancer_failed"])
            )
    return results


__all__ = [
    "EnhancementResult",
    "Enhancer",
    "GraphExtractionEnhancer",
    "GraphExtractionUnavailable",
    "ImageContextEnhancer",
    "ImageContextUnavailable",
    "available_enhancers",
    "describe_file_images",
    "docling_graph_installed",
    "extract_file_images",
    "graph_payload_from_text",
    "resolve_profile_template",
    "run_enhancements",
]
