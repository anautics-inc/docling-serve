"""Enhancement layer contract.

Every extractor produces a *default* bundle. Enhancers are optional, opt-in
passes that enrich that bundle in place — e.g. send each extracted image to a
vision agent and write the returned context back into the deep document. They
run after the base extraction and after assets are attached, so they can see
the final ``document.json`` and the media on disk.

Enhancers are requested per call (``enhancements`` form field) and never change
default behaviour when not requested. They mutate the passed ``document`` dict
and may write sidecar artifacts under ``ctx.bundle_dir``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from docling_serve.extractors.base import ExtractionContext, ExtractorResult


@dataclass(slots=True)
class EnhancementResult:
    name: str
    applied: bool = False
    artifacts: list[str] = field(default_factory=list)
    manifest_extra: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Enhancer(ABC):
    """Base class for opt-in post-extraction enrichment passes."""

    #: Identifier matched against the requested ``enhancements`` list.
    name: str = "enhancer"
    #: Alternate spellings callers may use.
    aliases: tuple[str, ...] = ()

    @abstractmethod
    def applies(self, ctx: ExtractionContext, document: dict[str, Any]) -> bool:
        """True when this enhancer can run for the given document."""

    @abstractmethod
    def enhance(
        self,
        ctx: ExtractionContext,
        document: dict[str, Any],
        *,
        base_result: ExtractorResult,
    ) -> EnhancementResult:
        """Enrich ``document`` in place; return what was added."""
