"""Per-request caller identity, threaded through the extraction pipeline.

captify-pytology (the Cognito-gated gateway in front of this service) forwards
the authenticated caller on every request as headers:

    x-captify-tenant-id   tenant the upload belongs to
    x-captify-actor-id    user (or agent) that initiated the call
    x-request-id          gateway correlation id

The HTTP layer stores these on the task metadata; the deep-extraction worker
re-binds them here (a ``ContextVar``) for the duration of a task so *every*
model call made anywhere in the pipeline — vision passes, knowledge-graph
extraction — is attributed to the originating user/tenant in LiteLLM spend
logs without threading identity arguments through every extractor signature.

Model calls still use the docling-serve service virtual key (the service is a
trusted internal pipeline; see the LiteLLM section of ``.env``) — identity
binding is for attribution and audit, not for credential selection.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """The authenticated caller forwarded by the gateway, if any."""

    tenant_id: str | None = None
    actor_id: str | None = None
    request_id: str | None = None

    @property
    def end_user(self) -> str | None:
        """Stable end-user id for LiteLLM spend attribution (``user`` field)."""
        if self.actor_id:
            return self.actor_id
        if self.tenant_id:
            return f"tenant:{self.tenant_id}"
        return None

    def tags(self) -> list[str]:
        """Spend-log tags identifying the caller (``x-litellm-tags`` header)."""
        tags: list[str] = []
        if self.tenant_id:
            tags.append(f"tenant:{self.tenant_id}")
        if self.actor_id:
            tags.append(f"actor:{self.actor_id}")
        return tags

    def headers(self) -> dict[str, str]:
        """Identity headers to forward on outbound LLM calls.

        The LiteLLM proxy records these as spend-log tags
        (``litellm_settings.extra_spend_tag_headers``), which is how usage on
        the shared docling-serve service key is isolated per user/tenant.
        """
        headers: dict[str, str] = {}
        if self.tenant_id:
            headers["x-captify-tenant-id"] = self.tenant_id
        if self.actor_id:
            headers["x-captify-actor-id"] = self.actor_id
        return headers


_current_identity: ContextVar[RequestIdentity | None] = ContextVar(
    "docling_request_identity", default=None
)


def current_identity() -> RequestIdentity | None:
    """Identity bound to the current context, or ``None`` for system work."""
    return _current_identity.get()


@contextmanager
def bind_identity(identity: RequestIdentity | None) -> Iterator[None]:
    """Bind ``identity`` for the duration of the block (no-op when ``None``)."""
    if identity is None:
        yield
        return
    token = _current_identity.set(identity)
    try:
        yield
    finally:
        _current_identity.reset(token)


def identity_from_task_metadata(metadata: dict | None) -> RequestIdentity | None:
    """Rebuild the caller identity captured by the HTTP layer at enqueue time."""
    metadata = metadata or {}
    tenant_id = str(metadata.get("tenant_id") or "") or None
    actor_id = str(metadata.get("actor_id") or "") or None
    request_id = str(metadata.get("request_id") or "") or None
    if not (tenant_id or actor_id or request_id):
        return None
    return RequestIdentity(
        tenant_id=tenant_id, actor_id=actor_id, request_id=request_id
    )
