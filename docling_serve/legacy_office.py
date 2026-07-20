"""Compatibility facade for :mod:`docling_serve.legacy`."""

import sys as _sys

from docling_serve.legacy import _implementation as _implementation

_sys.modules[__name__] = _implementation
