from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from docling_serve import legacy_office, upload_staging
from docling_serve.legacy import LegacyOfficeDoclingConverterManager
from docling_serve.settings import DoclingServeSettings
from docling_serve.staging import StagedUploadRef


def test_compatibility_facades_preserve_public_symbol_identity() -> None:
    assert upload_staging.StagedUploadRef is StagedUploadRef
    assert (
        legacy_office.LegacyOfficeDoclingConverterManager
        is LegacyOfficeDoclingConverterManager
    )


def test_schematic_extractor_and_revision_have_no_direct_cycle() -> None:
    root = Path("docling_serve/schematic")
    extractor = (root / "schematic_extractor.py").read_text()
    revision = (root / "schematic_revision.py").read_text()
    delivery = (root / "pipeline/delivery.py").read_text()
    rendering = (root / "pipeline/rendering.py").read_text()
    assert "schematic.schematic_revision import" not in extractor
    assert "schematic.schematic_extractor import" not in revision
    assert "schematic.schematic_revision import" not in delivery
    assert "schematic.schematic_extractor import" not in delivery
    assert "schematic.schematic_extractor import" not in rendering
    assert "schematic.schematic_revision import" not in rendering


def test_kicad_svg_exports_share_one_cli_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from docling_serve.schematic.pipeline import delivery, rendering

    commands: list[list[str]] = []
    monkeypatch.setattr(rendering, "ensure_kicad_cli_config", lambda: None)
    monkeypatch.setattr(
        rendering.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    schematic = tmp_path / "fixture.kicad_sch"
    rendering.export_kicad_svg(schematic, tmp_path, no_background_color=True)
    rendering.export_kicad_svg(schematic, tmp_path)
    assert "--no-background-color" in commands[0]
    assert "--no-background-color" not in commands[1]
    assert "export_kicad_svg(kicad_path, out)" in Path(delivery.__file__).read_text()


def test_flat_environment_aliases_feed_immutable_views(monkeypatch) -> None:
    monkeypatch.setenv("DOCLING_SERVE_UPLOAD_STAGING_BUCKET", "phase6-bucket")
    monkeypatch.setenv("DOCLING_SERVE_LEGACY_OFFICE_ENABLED", "false")
    monkeypatch.setenv("DOCLING_SERVE_GRAPH_EXTRACTION_MAX_CHARS", "1234")
    monkeypatch.setenv("DOCLING_SERVE_AUTO_ROUTE_MIN_PARTS_SIGNALS", "3")
    monkeypatch.setenv("DOCLING_SERVE_ARTIFACT_STORAGE_BUCKET", "artifact-bucket")
    monkeypatch.setenv("DOCLING_SERVE_ENG_LOC_NUM_WORKERS", "7")

    settings = DoclingServeSettings(_env_file=None)
    assert settings.upload_staging_bucket == settings.staging.bucket == "phase6-bucket"
    assert settings.legacy_office_enabled is settings.legacy_office.enabled is False
    assert settings.graph.max_chars == 1234
    assert settings.auto_routing.min_parts_signals == 3
    assert settings.artifacts.bucket == "artifact-bucket"
    assert settings.engine_adapters.local["num_workers"] == 7
    with pytest.raises(FrozenInstanceError):
        settings.staging.bucket = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        settings.engine_adapters.local["num_workers"] = 8  # type: ignore[index]


def test_curated_package_exports_are_closed_and_resolvable() -> None:
    import docling_serve.legacy as legacy
    import docling_serve.staging as staging

    for package in (legacy, staging):
        assert len(package.__all__) == len(set(package.__all__))
        assert all(hasattr(package, name) for name in package.__all__)
