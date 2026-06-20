"""Auth behavior for the IP-aware ``APIKeyAuth`` dependency + startup validator.

A key is required only for non-exempt callers: ``allow_unauthenticated`` exempts
everyone (dev/test), and ``allow_private_networks`` (default) exempts loopback/
private clients so a local install works with no key while public clients still
need one. These exercise the pure dependency with synthetic requests — no network.
"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from docling_serve.auth import APIKeyAuth, is_private_client
from docling_serve.settings import DoclingServeSettings


def _request(client_host: str | None, api_key_header: str | None = None) -> Request:
    headers = []
    if api_key_header is not None:
        headers.append((b"x-api-key", api_key_header.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("10.1.2.3", True),
        ("192.168.5.5", True),
        ("172.16.0.1", True),
        ("169.254.1.1", True),
        ("fd00::1", True),
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        (None, False),
        ("not-an-ip", False),
    ],
)
def test_is_private_client(host, expected):
    assert is_private_client(host) is expected


@pytest.mark.asyncio
async def test_private_client_needs_no_key():
    auth = APIKeyAuth("", allow_private_networks=True)
    assert (await auth(_request("127.0.0.1"))).valid is True


@pytest.mark.asyncio
async def test_private_client_bypasses_even_when_key_configured():
    auth = APIKeyAuth("s3cr3t", allow_private_networks=True)
    assert (await auth(_request("10.0.0.5"))).valid is True


@pytest.mark.asyncio
async def test_public_client_with_no_key_configured_is_rejected():
    auth = APIKeyAuth("", allow_private_networks=True)
    with pytest.raises(HTTPException) as exc:
        await auth(_request("8.8.8.8"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_public_client_requires_matching_key():
    auth = APIKeyAuth("s3cr3t", allow_private_networks=True)
    assert (await auth(_request("8.8.8.8", "s3cr3t"))).valid is True
    with pytest.raises(HTTPException):
        await auth(_request("8.8.8.8", "wrong"))
    with pytest.raises(HTTPException):
        await auth(_request("8.8.8.8"))


@pytest.mark.asyncio
async def test_allow_unauthenticated_exempts_public_clients():
    auth = APIKeyAuth("", allow_unauthenticated=True)
    assert (await auth(_request("8.8.8.8"))).valid is True


@pytest.mark.asyncio
async def test_private_bypass_disabled_requires_key_for_local():
    auth = APIKeyAuth("s3cr3t", allow_private_networks=False)
    assert (await auth(_request("127.0.0.1", "s3cr3t"))).valid is True
    with pytest.raises(HTTPException):
        await auth(_request("127.0.0.1"))


def test_validate_serving_auth_mode_ok_with_private_bypass():
    # No key, but private bypass on (default): local works, public is rejected.
    DoclingServeSettings(
        api_key=None, allow_unauthenticated=False
    ).validate_serving_auth_mode()


def test_validate_serving_auth_mode_ok_with_key():
    DoclingServeSettings(
        api_key="s3cr3t", auth_allow_private_networks=False
    ).validate_serving_auth_mode()


def test_validate_serving_auth_mode_fails_when_fully_locked():
    settings = DoclingServeSettings(
        api_key=None,
        allow_unauthenticated=False,
        auth_allow_private_networks=False,
    )
    with pytest.raises(ValueError):
        settings.validate_serving_auth_mode()
