import mimetypes
import tempfile
from functools import lru_cache
from pathlib import Path

from docling_serve.settings import docling_serve_settings


@lru_cache
def get_scratch() -> Path:
    scratch_dir = (
        docling_serve_settings.scratch_path
        if docling_serve_settings.scratch_path is not None
        else Path(tempfile.mkdtemp(prefix="docling_"))
    )
    scratch_dir.mkdir(exist_ok=True, parents=True)
    return scratch_dir


#: Canonical Content-Type per artifact suffix the pipeline emits. Pinned
#: explicitly (mimetypes only as a last resort for unforeseen files) because
#: the stdlib table varies by host OS — the type an object carries at rest
#: must not depend on which machine published it.
_EXTRA_CONTENT_TYPES: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".xml": "application/xml",
    ".kbl": "application/xml",
    ".csv": "text/csv; charset=utf-8",
    ".edml": "text/plain; charset=utf-8",
    ".net": "text/plain; charset=utf-8",
    ".cir": "text/plain; charset=utf-8",
    ".kicad_sch": "text/plain; charset=utf-8",
    ".kicad_pro": "application/json",
    ".edb": "application/octet-stream",
}

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def content_type_for(name: str | Path) -> str:
    """The Content-Type an artifact must carry at rest in S3.

    Objects stored without a type default to ``binary/octet-stream``, and
    same-origin proxies that serve them set ``X-Content-Type-Options:
    nosniff`` — so an untyped SVG/PNG is refused by browsers in image
    contexts. Typing the object once at upload is the durable fix; every
    consumer (browser, proxy, presigned download) then agrees.
    """
    text = str(name).lower()
    for suffix, content_type in _EXTRA_CONTENT_TYPES.items():
        if text.endswith(suffix):
            return content_type
    guessed, _ = mimetypes.guess_type(text)
    return guessed or _DEFAULT_CONTENT_TYPE
