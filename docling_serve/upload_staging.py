"""Compatibility facade for :mod:`docling_serve.staging`."""

import sys as _sys

from docling_serve.staging import _implementation as _implementation

_sys.modules[__name__] = _implementation
