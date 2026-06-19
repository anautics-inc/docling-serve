"""Microsoft Access (.mdb/.accdb) extraction — a docling gap filled by converting
the database to docling-native markdown tables via mdbtools."""

from docling_serve.access.extract import (
    ACCESS_SUFFIXES,
    AccessToolsUnavailableError,
    access_to_markdown,
    is_access_file,
    mdbtools_available,
)

__all__ = [
    "ACCESS_SUFFIXES",
    "AccessToolsUnavailableError",
    "access_to_markdown",
    "is_access_file",
    "mdbtools_available",
]
