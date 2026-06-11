"""Unit tests for the LibreOffice legacy-Office pre-conversion (issue A1).

The soffice subprocess boundary is mocked so the suite validates the
conversion logic (temp-dir isolation + cleanup, typed errors, filename
mapping) without LibreOffice installed. When soffice IS available, a real
round-trip test generates legacy fixtures from the modern test files and
converts them back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from docling_serve.extraction import legacy_office
from docling_serve.extraction.legacy_office import (
    LEGACY_OFFICE_TARGETS,
    LegacyOfficeConversionError,
    convert_legacy_office_bytes,
    converted_filename,
    is_legacy_office,
    soffice_available,
)

TEST_FILES = Path(__file__).parent / "test_files"


# --------------------------------------------------------------------------- #
# Suffix mapping / dispatch                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.doc", True),
        ("Workbook.XLS", True),
        ("slides.ppt", True),
        ("report.docx", False),
        ("workbook.xlsx", False),
        ("slides.pptx", False),
        ("scan.pdf", False),
        ("notes.txt", False),
        ("", False),
        (None, False),
    ],
)
def test_is_legacy_office(name, expected):
    assert is_legacy_office(name) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.doc", "report.docx"),
        ("Work Book.XLS", "Work Book.xlsx"),
        ("slides.ppt", "slides.pptx"),
    ],
)
def test_converted_filename_preserves_stem(name, expected):
    assert converted_filename(name) == expected


# --------------------------------------------------------------------------- #
# Conversion via mocked soffice                                               #
# --------------------------------------------------------------------------- #


class _FakeSoffice:
    """Stands in for subprocess.run; optionally writes the converted file."""

    def __init__(self, *, returncode: int = 0, write_output: bool = True,
                 stderr: str = ""):
        self.returncode = returncode
        self.write_output = write_output
        self.stderr = stderr
        self.work_dirs: list[Path] = []

    def __call__(self, args, **kwargs):
        outdir = Path(args[args.index("--outdir") + 1])
        self.work_dirs.append(outdir)
        input_path = Path(args[-1])
        convert_to = args[args.index("--convert-to") + 1]
        if self.write_output:
            (outdir / f"{input_path.stem}.{convert_to}").write_bytes(
                b"converted:" + input_path.read_bytes()
            )
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode, stdout="", stderr=self.stderr
        )


def test_convert_legacy_office_bytes_success_and_cleanup(monkeypatch):
    fake = _FakeSoffice()
    monkeypatch.setattr(legacy_office, "_soffice_binary", lambda: "soffice")
    monkeypatch.setattr(legacy_office.subprocess, "run", fake)

    name, data = convert_legacy_office_bytes("My Report.doc", b"legacy-bytes")

    assert name == "My Report.docx"
    assert data == b"converted:legacy-bytes"
    # The isolated work dir is removed after a successful conversion.
    assert len(fake.work_dirs) == 1
    assert not fake.work_dirs[0].exists()


def test_convert_legacy_office_bytes_failure_is_typed_and_cleans_up(monkeypatch):
    fake = _FakeSoffice(returncode=1, write_output=False, stderr="source file could not be loaded")
    monkeypatch.setattr(legacy_office, "_soffice_binary", lambda: "soffice")
    monkeypatch.setattr(legacy_office.subprocess, "run", fake)

    with pytest.raises(LegacyOfficeConversionError) as err:
        convert_legacy_office_bytes("corrupt.xls", b"\x00\x01garbage")

    # The message names the conversion step, the file, and the soffice detail.
    message = str(err.value)
    assert "LibreOffice conversion" in message
    assert "corrupt.xls" in message
    assert "source file could not be loaded" in message
    # The isolated work dir is removed on failure too.
    assert not fake.work_dirs[0].exists()


def test_convert_legacy_office_bytes_no_output_is_typed(monkeypatch):
    # soffice exits 0 but produces nothing (a real failure mode).
    fake = _FakeSoffice(returncode=0, write_output=False)
    monkeypatch.setattr(legacy_office, "_soffice_binary", lambda: "soffice")
    monkeypatch.setattr(legacy_office.subprocess, "run", fake)

    with pytest.raises(LegacyOfficeConversionError, match="LibreOffice conversion"):
        convert_legacy_office_bytes("slides.ppt", b"junk")
    assert not fake.work_dirs[0].exists()


def test_convert_legacy_office_bytes_timeout_is_typed(monkeypatch):
    def _timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(legacy_office, "_soffice_binary", lambda: "soffice")
    monkeypatch.setattr(legacy_office.subprocess, "run", _timeout)

    with pytest.raises(LegacyOfficeConversionError, match="timed out"):
        convert_legacy_office_bytes("report.doc", b"junk")


def test_convert_legacy_office_bytes_requires_soffice(monkeypatch):
    monkeypatch.setattr(legacy_office, "_soffice_binary", lambda: None)
    with pytest.raises(LegacyOfficeConversionError, match="soffice"):
        convert_legacy_office_bytes("report.doc", b"junk")


def test_convert_legacy_office_bytes_rejects_modern_suffix(monkeypatch):
    monkeypatch.setattr(legacy_office, "_soffice_binary", lambda: "soffice")
    with pytest.raises(LegacyOfficeConversionError, match="not a legacy Office format"):
        convert_legacy_office_bytes("report.docx", b"modern")


# --------------------------------------------------------------------------- #
# Real soffice round trip (skipped when LibreOffice is not installed)         #
# --------------------------------------------------------------------------- #

_MODERN_FIXTURES = {
    ".doc": TEST_FILES / "generated-code-validation-procedures.docx",
    ".xls": TEST_FILES / "generated-training-workbook.xlsx",
    ".ppt": TEST_FILES / "1d7087c1-e49f-43e8-b383-7992e0bf8edb-SPM-Welcome-Page-Highlights.pptx",
}


@pytest.mark.skipif(not soffice_available(), reason="LibreOffice (soffice) not installed")
@pytest.mark.parametrize("legacy_suffix", sorted(LEGACY_OFFICE_TARGETS))
def test_real_soffice_round_trip(tmp_path, legacy_suffix):
    """Generate a legacy fixture from a modern one, then pre-convert it back."""
    modern_fixture = _MODERN_FIXTURES[legacy_suffix]
    assert modern_fixture.is_file(), f"missing fixture {modern_fixture}"

    # Modern -> legacy via soffice (fixture generation).
    completed = subprocess.run(
        [
            legacy_office._soffice_binary(),
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{tmp_path / 'profile'}",
            "--convert-to",
            legacy_suffix.lstrip("."),
            "--outdir",
            str(tmp_path),
            str(modern_fixture),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    legacy_path = tmp_path / f"{modern_fixture.stem}{legacy_suffix}"
    assert completed.returncode == 0 and legacy_path.is_file(), completed.stderr

    # Legacy -> modern via the unit under test.
    name, data = convert_legacy_office_bytes(legacy_path.name, legacy_path.read_bytes())
    assert name == f"{modern_fixture.stem}{LEGACY_OFFICE_TARGETS[legacy_suffix]}"
    assert len(data) > 0


@pytest.mark.skipif(not soffice_available(), reason="LibreOffice (soffice) not installed")
def test_real_soffice_corrupt_file_fails_typed(tmp_path):
    with pytest.raises(LegacyOfficeConversionError, match="LibreOffice conversion"):
        convert_legacy_office_bytes("corrupt.doc", b"\x00\x01\x02 not a doc file")
