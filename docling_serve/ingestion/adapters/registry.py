"""Authoritative executable adapter registry."""

from __future__ import annotations

import inspect
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from docling_serve.capabilities import CAPABILITIES, DocumentCapability, DocumentDomain
from docling_serve.ingestion.adapters.protocol import AdapterHandler

ReadinessProbe = Callable[[], bool]


def _ready() -> bool:
    return True


def _legacy_office_ready() -> bool:
    try:
        from docling_serve.legacy_office import check_legacy_office_capability

        check_legacy_office_capability()
    except (ImportError, OSError, RuntimeError):
        return False
    return True


def _access_ready() -> bool:
    return bool(_access_readiness()["available"])


def _form_ready() -> bool:
    return _imports("pikepdf", "docling_serve.form.extract")


def _technical_order_ready() -> bool:
    return _imports(
        "docling_serve.technical_order.extract",
        "docling_serve.technical_order.pdftext",
    ) and _executables("pdftotext", "pdftoppm")


def _schematic_ready() -> bool:
    return bool(_schematic_readiness()["core"])


def _imports(*modules: str) -> bool:
    try:
        for module in modules:
            import_module(module)
    except (ImportError, OSError):
        return False
    return True


def _executables(*names: str) -> bool:
    return all(shutil.which(name) is not None for name in names)


def _graph_ready() -> bool:
    try:
        from docling_serve.graph.extraction import (
            build_graph_config,
            docling_graph_installed,
        )
        from docling_serve.settings import docling_serve_settings
    except ImportError:
        return False
    return (
        docling_serve_settings.graph_extraction_enabled
        and docling_graph_installed()
        and build_graph_config() is not None
    )


@dataclass(frozen=True, slots=True)
class RegisteredAdapter:
    """Executable behavior paired with a public capability declaration."""

    capability: DocumentCapability
    handler_name: str
    readiness_probe: ReadinessProbe

    @property
    def domain(self) -> DocumentDomain:
        return self.capability.name

    def readiness(self) -> bool:
        return self.readiness_probe()

    def admission_limit(self, settings: Any) -> int:
        if self.domain == "legacy-office":
            return min(
                int(settings.max_file_size),
                int(settings.legacy_office_max_input_bytes),
            )
        return int(settings.max_file_size)

    async def extract(
        self,
        handlers: Mapping[str, AdapterHandler],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            handler = handlers[self.handler_name]
        except KeyError as exc:
            raise RuntimeError(
                f"No extraction handler is bound for adapter {self.domain!r}"
            ) from exc
        result = handler(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


_READINESS: dict[DocumentDomain, ReadinessProbe] = {
    "document": _ready,
    "legacy-office": _legacy_office_ready,
    "access": _access_ready,
    "form": _form_ready,
    "technical-order": _technical_order_ready,
    "schematic": _schematic_ready,
    "graph-extraction": _graph_ready,
}

ADAPTERS: Mapping[DocumentDomain, RegisteredAdapter] = {
    domain: RegisteredAdapter(
        capability=capability,
        handler_name=capability.runtime_adapter or domain,
        readiness_probe=_READINESS[domain],
    )
    for domain, capability in CAPABILITIES.items()
}


def get_adapter(domain: DocumentDomain) -> RegisteredAdapter:
    return ADAPTERS[domain]


async def execute_adapter(
    domain: DocumentDomain,
    handlers: Mapping[str, AdapterHandler],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    return await get_adapter(domain).extract(handlers, *args, **kwargs)


def adapter_readiness() -> dict[str, bool]:
    return {domain: adapter.readiness() for domain, adapter in ADAPTERS.items()}


def _access_readiness() -> dict[str, bool]:
    parser = _imports("access_parser", "docling_serve.access.extract")
    jackcess = bool(os.getenv("DOCLING_SERVE_JACKCESS_CLASSPATH", "").strip()) and (
        _executables("java", "javac")
    )
    return {
        "available": parser or jackcess,
        "access_parser": parser,
        "jackcess": jackcess,
    }


def _schematic_readiness() -> dict[str, bool]:
    core = _imports(
        "docling_serve.schematic.extract",
        "docling_serve.schematic.schematic_extractor",
    ) and _executables("pdftocairo")
    kicad = _executables("kicad-cli")
    return {"core": core, "kicad_export": kicad, "kicad_erc": kicad}


def adapter_readiness_details() -> dict[str, dict[str, bool]]:
    """Expose optional runtime components without marking core extraction down."""

    return {
        "access": _access_readiness(),
        "schematic": _schematic_readiness(),
    }


def public_capabilities() -> list[dict[str, object]]:
    readiness = adapter_readiness()
    return [
        {
            **adapter.capability.public_dict(),
            "available": readiness[domain],
        }
        for domain, adapter in ADAPTERS.items()
    ]
