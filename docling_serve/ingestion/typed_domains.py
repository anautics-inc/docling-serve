"""Shared typed-domain extraction services for sync and canonical ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def extract_access_domain(path: Path, *, source_key: str) -> dict[str, Any]:
    from docling_serve.access.extract import dump_schema, extract_access

    markdown, tables, tabular_tables = extract_access(path)
    return {
        "filename": source_key,
        "markdown": markdown,
        "tables": tables,
        "schema": dump_schema(path),
        "tabular": {
            "format": "captify.access/v1",
            "tables": tabular_tables,
        },
    }


def extract_form_domain(
    path: Path, *, source_key: str, include_packets: bool = False
) -> dict[str, Any]:
    from docling_serve.form import extract_xfa_form

    payload = extract_xfa_form(path, source_key=source_key)
    if not include_packets:
        return payload
    from docling_serve.form import read_xfa_packets

    return {**payload, "_packets": read_xfa_packets(path)}


def public_form_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "_packets"}


def extract_technical_order_domain(
    path: Path,
    *,
    source_key: str,
    media_dir: Path | None = None,
    vision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from docling_serve.technical_order.extract import extract_technical_order

    return extract_technical_order(
        path,
        source_key=source_key,
        media_dir=media_dir,
        vision=vision,
    )


def extract_schematic_domain(
    path: Path,
    bundle: Path,
    *,
    profile: str,
    tenant_id: str,
    source_key: str,
) -> dict[str, Any]:
    from docling_serve.schematic.extract import extract_schematic

    return extract_schematic(
        path,
        bundle,
        profile=profile,
        tenant_id=tenant_id,
        source_key=source_key,
    )
