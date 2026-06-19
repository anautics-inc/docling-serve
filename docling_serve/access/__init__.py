"""Microsoft Access (.mdb/.accdb) extraction — a docling gap filled by converting
the database to docling-native markdown tables via the pure-Python access-parser."""

from docling_serve.access.extract import (
    ACCESS_SUFFIXES,
    AccessToolsUnavailableError,
    access_to_markdown,
    dump_schema,
    is_access_file,
)

__all__ = [
    "ACCESS_SUFFIXES",
    "AccessToolsUnavailableError",
    "access_to_markdown",
    "dump_schema",
    "is_access_file",
]
