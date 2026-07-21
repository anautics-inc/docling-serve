from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from docling_serve.access import extract as access_extract
from docling_serve.execution.failure_mapping import map_task_failure
from docling_serve.execution.subprocesses import (
    ExternalCommandError,
    run_external,
)
from docling_serve.ingestion.adapters import registry
from scripts.enrich_sbom import MAVEN_COMPONENTS, enrich


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def test_external_command_policy_rejects_unknown_executables() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        run_external(["sh", "-c", "echo unsafe"], timeout=1)


def test_external_command_errors_do_not_expose_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("secret-token")

    monkeypatch.setattr(
        "docling_serve.execution.subprocesses.subprocess.run",
        missing,
    )
    with pytest.raises(ExternalCommandError) as error:
        run_external(["java", "--password=secret-token"], timeout=1)
    assert "secret-token" not in str(error.value)
    assert map_task_failure(error.value).service_owned is True


def test_jackcess_fallback_emits_markdown_and_tabular_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    output = "\n".join(
        (
            "\t".join(("H", _encoded("Items"), _encoded("id"), _encoded("name"))),
            "\t".join(("R", _encoded("Items"), _encoded("1"), _encoded("Widget"))),
        )
    )
    monkeypatch.setenv("DOCLING_SERVE_JACKCESS_CLASSPATH", "/opt/jackcess/*")
    monkeypatch.setattr(
        access_extract,
        "run_external",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
    )
    database = tmp_path / "inventory.accdb"
    database.write_bytes(b"fixture")

    markdown, summaries, tabular = access_extract._jackcess_extract(database)

    assert "| 1 | Widget |" in markdown
    assert summaries == [{"name": "Items", "columns": 2, "rows": 1}]
    assert tabular[0]["rows"] == [{"id": "1", "name": "Widget"}]


def test_readiness_separates_schematic_core_from_optional_kicad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry, "_imports", lambda *_names: True)
    monkeypatch.setattr(
        registry,
        "_executables",
        lambda *names: "kicad-cli" not in names,
    )

    details = registry.adapter_readiness_details()

    assert details["schematic"] == {
        "core": True,
        "kicad_export": False,
        "kicad_erc": False,
    }


def test_sbom_includes_every_pinned_jackcess_runtime_component() -> None:
    payload = enrich({"components": []})
    purls = {component["purl"] for component in payload["components"]}
    assert purls == {
        f"pkg:maven/{group}/{name}@{version}"
        for group, name, version in MAVEN_COMPONENTS
    }
