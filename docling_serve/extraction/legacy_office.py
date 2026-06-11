"""LibreOffice pre-conversion for legacy binary Office formats.

Docling has no backends for the legacy binary Office formats (``.doc`` /
``.xls`` / ``.ppt``), so those uploads used to die with a generic conversion
error. This module converts them to their modern OOXML equivalents
(``.docx`` / ``.xlsx`` / ``.pptx``) with headless LibreOffice *before* the
file enters the normal docling + extractor chain, so a legacy upload extracts
end-to-end exactly like a modern one.

Each conversion runs in an isolated temp dir (with its own LibreOffice user
profile so parallel conversions cannot clash) that is removed on success and
failure. Conversion problems raise :class:`LegacyOfficeConversionError` — a
typed, user-readable error naming the conversion step — never a generic
docling failure.

Requires the LibreOffice headless OS packages (see ``os-packages.txt``).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

_log = logging.getLogger(__name__)

#: Legacy binary suffix -> modern OOXML suffix (and soffice convert-to filter).
LEGACY_OFFICE_TARGETS: dict[str, str] = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}

#: Seconds to wait for one soffice conversion before declaring it stuck.
SOFFICE_TIMEOUT_SECONDS = 300


class LegacyOfficeConversionError(RuntimeError):
    """LibreOffice pre-conversion of a legacy Office file failed.

    The message always names the conversion step and the offending file so
    the caller (and the user) can tell a format problem from a generic
    docling failure.
    """


def soffice_available() -> bool:
    """True when the LibreOffice CLI is installed."""
    return _soffice_binary() is not None


def is_legacy_office(filename: str | None) -> bool:
    """True when ``filename`` has a legacy binary Office suffix."""
    if not filename:
        return False
    return Path(filename).suffix.lower() in LEGACY_OFFICE_TARGETS


def converted_filename(filename: str) -> str:
    """The modern-format name for a legacy ``filename`` (stem preserved)."""
    path = Path(filename)
    target_suffix = LEGACY_OFFICE_TARGETS[path.suffix.lower()]
    return f"{path.stem}{target_suffix}"


def convert_legacy_office_bytes(filename: str, data: bytes) -> tuple[str, bytes]:
    """Convert legacy Office ``data`` to its modern equivalent via soffice.

    Returns ``(converted_filename, converted_bytes)``. The work happens in an
    isolated temp dir (own LibreOffice profile) which is always cleaned up.
    Raises :class:`LegacyOfficeConversionError` when LibreOffice is missing,
    times out, exits non-zero, or produces no output file.
    """
    source = Path(filename)
    suffix = source.suffix.lower()
    if suffix not in LEGACY_OFFICE_TARGETS:
        raise LegacyOfficeConversionError(
            f"LibreOffice pre-conversion does not apply to '{filename}': "
            f"not a legacy Office format ({', '.join(sorted(LEGACY_OFFICE_TARGETS))})."
        )

    soffice = _soffice_binary()
    if soffice is None:
        raise LegacyOfficeConversionError(
            f"LibreOffice pre-conversion of '{filename}' is unavailable: "
            "the 'soffice' binary is not installed (see os-packages.txt)."
        )

    target_suffix = LEGACY_OFFICE_TARGETS[suffix]
    convert_to = target_suffix.lstrip(".")
    work_dir = Path(tempfile.mkdtemp(prefix="legacy-office-"))
    try:
        input_path = work_dir / source.name
        input_path.write_bytes(data)
        profile_dir = work_dir / "profile"
        try:
            completed = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--convert-to",
                    convert_to,
                    "--outdir",
                    str(work_dir),
                    str(input_path),
                ],
                capture_output=True,
                text=True,
                timeout=SOFFICE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as err:
            raise LegacyOfficeConversionError(
                f"LibreOffice conversion of '{filename}' to {convert_to} timed out "
                f"after {SOFFICE_TIMEOUT_SECONDS}s."
            ) from err

        output_path = work_dir / f"{source.stem}{target_suffix}"
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise LegacyOfficeConversionError(
                f"LibreOffice conversion of '{filename}' to {convert_to} failed: "
                f"{detail or 'no output produced'}. "
                "The file may be corrupt or not a valid legacy Office document."
            )
        return output_path.name, output_path.read_bytes()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _soffice_binary() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")
