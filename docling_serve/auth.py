import hmac
import ipaddress
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel


class AuthenticationResult(BaseModel):
    valid: bool
    errors: list[str] = []
    detail: Any | None = None


def is_private_client(host: str | None) -> bool:
    """True when the request's direct peer is loopback or a private address.

    Covers loopback, RFC1918 private, link-local, and IPv6 unique-local (ULA)
    ranges. Uses the socket peer only (``request.client.host``); it deliberately
    does NOT consult ``X-Forwarded-For`` because that header is client-spoofable.
    Behind a reverse proxy the peer is the proxy, so an operator who wants per-
    external-client auth must disable ``auth_allow_private_networks`` and set a key.
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class APIKeyAuth(APIKeyHeader):
    """FastAPI dependency evaluating the ``X-Api-Key`` header.

    A key is required only for requests that are not exempt:
      * ``allow_unauthenticated`` exempts every request (dev/test);
      * ``allow_private_networks`` (default) exempts loopback/private clients, so
        a local install works with no key while public clients still need one.
    """

    def __init__(
        self,
        api_key: str,
        header_name: str = "X-Api-Key",
        *,
        allow_unauthenticated: bool = False,
        allow_private_networks: bool = True,
    ) -> None:
        self.api_key = api_key
        self.header_name = header_name
        self.allow_unauthenticated = allow_unauthenticated
        self.allow_private_networks = allow_private_networks
        super().__init__(name=self.header_name, auto_error=False)

    def request_requires_key(self, request: Request) -> bool:
        """Whether this caller must present a valid key.

        ``request`` may be a ``Request`` or a ``WebSocket`` — both expose
        ``.client.host``.
        """
        if self.allow_unauthenticated:
            return False
        client = getattr(request, "client", None)
        client_host = client.host if client else None
        if self.allow_private_networks and is_private_client(client_host):
            return False
        return True

    async def _validate_api_key(self, header_api_key: str | None):
        if header_api_key is None:
            return AuthenticationResult(
                valid=False, errors=[f"Missing header {self.header_name}."]
            )

        header_api_key = header_api_key.strip()

        if self.api_key and hmac.compare_digest(header_api_key, self.api_key):
            return AuthenticationResult(valid=True, detail=header_api_key)
        return AuthenticationResult(
            valid=False, errors=["The provided API Key is invalid."]
        )

    async def __call__(self, request: Request) -> AuthenticationResult:  # type: ignore
        if not self.request_requires_key(request):
            return AuthenticationResult(valid=True)

        if not self.api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required for non-local access.",
            )

        header_api_key = await super().__call__(request=request)
        result = await self._validate_api_key(header_api_key)
        if not result.valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key.",
            )
        return result
