"""Bounded PDF probes shared by admission and schematic extraction."""

from __future__ import annotations

import re
import zlib


def looks_like_vector_pdf(
    payload: bytes,
    *,
    max_streams: int,
    max_stream_output_bytes: int,
    max_total_output_bytes: int,
    min_path_operators: int = 201,
) -> bool:
    """Return whether bounded PDF content contains vector-drawing signals."""
    if not payload or b"/Subtype /Image" in payload or b"/Subtype/Image" in payload:
        return False
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", payload, re.S)
    path_ops = 0
    total_output = 0
    for stream in streams[:max_streams]:
        remaining = max_total_output_bytes - total_output
        if remaining <= 0:
            break
        limit = min(max_stream_output_bytes, remaining)
        try:
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(stream, limit + 1)
        except zlib.error:
            continue
        if len(decoded) > limit or decompressor.unconsumed_tail:
            continue
        total_output += len(decoded)
        for operator in (
            rb"(?<![A-Za-z])l(?![A-Za-z])",
            rb"(?<![A-Za-z])c(?![A-Za-z])",
        ):
            path_ops += len(re.findall(operator, decoded))
        if path_ops >= min_path_operators:
            return True
    return False
