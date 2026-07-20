"""Extractor contract shared by every document type.

An *extractor* turns one source document into the structured ``document.json``
payload (the deep-document object the rest of the pipeline consumes) and may
emit extra, domain-specific sidecar artifacts into the bundle directory
(e.g. a schematic's SVG + netlist, an Access DB's per-table CSVs).

The dispatch seam is :func:`select_extractor`, which picks an extractor from
the registry by ``(profile, file suffix, content)``. Adding a new format means
writing one :class:`Extractor` and registering it — no edits to the assembly
code in the native schematic bundle exporter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ExtractionContext:
    """Everything an extractor needs for one document.

    ``conv_res`` is Docling's :class:`ConversionResult` (``None`` when the source
    bypassed Docling, e.g. an Access database expanded by a connector).
    ``source_dir`` holds the original uploaded bytes so native extractors can
    re-open the true file by name.
    """

    source_path: Path
    bundle_dir: Path
    media_dir: Path
    source_manifest_key: str
    task_id: str
    profile: str = "default"
    conv_res: Any | None = None
    source_dir: Path | None = None
    #: Opt-in enhancers requested for this call (e.g. ``["image_context"]``).
    #: Each extractor's base output runs first; enhancers then enrich it.
    enhancements: list[str] = field(default_factory=list)
    #: Optional live progress sink ``(stage, detail) -> None``. Extractors call
    #: :meth:`report_progress` at meaningful steps (e.g. the schematic
    #: extractor's geometry/model/tracing stages) so UIs can show what the
    #: extraction is doing while it runs. Never required, never raises.
    progress: Any | None = None

    def report_progress(self, stage: str, **detail: Any) -> None:
        """Report a named extraction stage to the progress sink, if any."""
        if self.progress is None:
            return
        try:
            self.progress(stage, detail or None)
        except Exception:
            pass

    def resolve_source_file(self) -> Path:
        """Best path to the real source bytes on disk.

        Docling streams uploads, so ``conv_res.input.file`` is a bare name.
        Prefer the scratch copy under ``source_dir``; fall back to
        ``source_path`` when it already points at real bytes.
        """
        if self.source_path.is_file():
            return self.source_path
        if self.source_dir is not None:
            candidate = self.source_dir / self.source_path.name
            if candidate.is_file():
                return candidate
        return self.source_path


@dataclass(slots=True)
class ExtractorResult:
    """Output of an extractor for one document.

    ``structured`` is the validated deep-document dict written to
    ``document.json``. ``manifest_extra`` is merged into ``extraction.json`` so
    downstream consumers can discover domain artifacts (e.g. the schematic
    graph/netlist paths) without guessing filenames.
    """

    structured: dict[str, Any]
    extractor: str
    domain: str | None = None
    artifacts: list[str] = field(default_factory=list)
    manifest_extra: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Extractor(ABC):
    """Base class for all extractors.

    Subclasses declare the file suffixes / profiles they own via
    :meth:`supports` and produce a bundle via :meth:`build`. Keep model and
    vendor specifics behind providers — extractors orchestrate, they do not
    embed prompts or SDK calls inline.
    """

    #: Stable identifier reported in ``extraction.json`` (``extractor`` field).
    name: str = "extractor"

    #: False for extractors that read the source bytes natively and never need
    #: a docling :class:`ConversionResult` (e.g. Access via mdbtools). These
    #: extractors keep working when docling cannot convert the source at all,
    #: so the chunk pipeline and bundle assembly accept their results even when
    #: the docling conversion failed or was skipped.
    requires_docling: bool = True

    @abstractmethod
    def supports(self, ctx: ExtractionContext) -> bool:
        """True when this extractor should handle ``ctx``."""

    @abstractmethod
    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        """Produce the structured document + any sidecar artifacts."""
