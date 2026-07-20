"""Types for executable ingestion adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

AdapterHandler = Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]
