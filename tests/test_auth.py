"""Tests for the ``X-Api-Key`` dependency's fail-closed posture.

With no ``DOCLING_SERVE_API_KEY`` configured, every request must be refused
(503) unless the deployment explicitly opts into unauthenticated access via
``allow_no_auth`` — the misconfigured-production case must never silently
degrade to "everything is public".
"""

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from docling_serve.auth import APIKeyAuth, AuthenticationResult, MachineAssertionAuth


def _make_app(**auth_kwargs) -> FastAPI:
    app = FastAPI()
    require_auth = APIKeyAuth(**auth_kwargs)

    @app.get("/protected")
    def protected(auth: AuthenticationResult = Depends(require_auth)):
        return {"ok": True}

    return app


def test_no_api_key_fails_closed_by_default():
    """No key configured, no explicit opt-in: every request is refused."""
    client = TestClient(_make_app(api_key=""))

    resp = client.get("/protected")
    assert resp.status_code == 503

    # Even a caller who supplies SOME header still gets refused — there is
    # no configured key to validate against either way.
    resp = client.get("/protected", headers={"X-Api-Key": "anything"})
    assert resp.status_code == 503


def test_no_api_key_with_explicit_allow_no_auth_opt_in():
    """Explicit dev/test opt-in restores the permissive open-access behavior."""
    client = TestClient(_make_app(api_key="", allow_no_auth=True))

    resp = client.get("/protected")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # No header required either.
    resp = client.get("/protected", headers={"X-Api-Key": "whatever"})
    assert resp.status_code == 200


def test_configured_api_key_rejects_missing_or_wrong_header():
    client = TestClient(_make_app(api_key="secret-123"))

    resp = client.get("/protected")
    assert resp.status_code == 401

    resp = client.get("/protected", headers={"X-Api-Key": "wrong"})
    assert resp.status_code == 401


def test_configured_api_key_accepts_correct_header():
    client = TestClient(_make_app(api_key="secret-123"))

    resp = client.get("/protected", headers={"X-Api-Key": "secret-123"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_configured_api_key_ignores_allow_no_auth():
    """``allow_no_auth`` only matters when no key is configured."""
    client = TestClient(_make_app(api_key="secret-123", allow_no_auth=True))

    resp = client.get("/protected")
    assert resp.status_code == 401

    resp = client.get("/protected", headers={"X-Api-Key": "secret-123"})
    assert resp.status_code == 200


class _ReplayStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.available = True

    def consume(self, jti: str, ttl_seconds: int) -> bool:
        assert 0 < ttl_seconds <= 300
        if not self.available:
            raise ConnectionError("redis unavailable")
        if jti in self.seen:
            return False
        self.seen.add(jti)
        return True


@pytest.fixture
def assertion_contract():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    replay = _ReplayStore()
    auth = MachineAssertionAuth(
        issuer="captify-pytology",
        audience="docling-service",
        client_id="captify-platform",
        algorithm="RS256",
        public_key=public_pem.decode(),
        replay_store=replay,
    )
    app = FastAPI()

    @app.post("/v1/graph/extract")
    def protected(result: AuthenticationResult = Depends(auth)):
        return {"valid": result.valid}

    def mint(**overrides):
        now = int(time.time())
        claims = {
            "iss": "captify-pytology",
            "aud": "docling-service",
            "sub": "user-1",
            "tid": "tenant-a",
            "cid": "captify-platform",
            "res": "document:tenant-a:doc-1:/v1/graph/extract",
            "act": "post",
            "iat": now,
            "nbf": now,
            "exp": now + 120,
            "jti": uuid.uuid4().hex,
        }
        claims.update(overrides)
        return jwt.encode(claims, private_pem, algorithm="RS256")

    return TestClient(app), mint, replay


def _assertion_headers(token: str, **overrides: str) -> dict[str, str]:
    headers = {
        "x-captify-identity-assertion": token,
        "x-captify-tenant-id": "tenant-a",
        "X-Tenant-Id": "tenant-a",
        "x-captify-document-id": "doc-1",
    }
    headers.update(overrides)
    return headers


def test_machine_assertion_accepts_exact_request_binding(assertion_contract):
    client, mint, _replay = assertion_contract
    response = client.post("/v1/graph/extract", headers=_assertion_headers(mint()))
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("claim", "value", "status_code"),
    [
        ("iss", "other-issuer", 401),
        ("aud", "other-service", 401),
        ("cid", "other-client", 403),
        ("tid", "other-tenant", 403),
        ("res", "document:tenant-a:doc-2:/v1/graph/extract", 403),
        ("res", "document:tenant-a:doc-1:/v1/result/task-1", 403),
        ("act", "get", 403),
    ],
)
def test_machine_assertion_rejects_scope_substitution(
    assertion_contract, claim, value, status_code
):
    client, mint, _replay = assertion_contract
    response = client.post(
        "/v1/graph/extract",
        headers=_assertion_headers(mint(**{claim: value})),
    )
    assert response.status_code == status_code


def test_machine_assertion_rejects_overlong_lifetime(assertion_contract):
    client, mint, _replay = assertion_contract
    now = int(time.time())
    response = client.post(
        "/v1/graph/extract",
        headers=_assertion_headers(mint(iat=now, nbf=now, exp=now + 301)),
    )
    assert response.status_code == 401


def test_machine_assertion_rejects_operational_tenant_substitution(assertion_contract):
    client, mint, _replay = assertion_contract
    response = client.post(
        "/v1/graph/extract",
        headers=_assertion_headers(mint(), **{"X-Tenant-Id": "victim-tenant"}),
    )
    assert response.status_code == 401


def test_machine_assertion_is_single_use_across_shared_store(assertion_contract):
    client, mint, _replay = assertion_contract
    headers = _assertion_headers(mint())
    assert client.post("/v1/graph/extract", headers=headers).status_code == 200
    assert client.post("/v1/graph/extract", headers=headers).status_code == 401


def test_machine_assertion_replay_store_outage_fails_closed(assertion_contract):
    client, mint, replay = assertion_contract
    replay.available = False
    response = client.post("/v1/graph/extract", headers=_assertion_headers(mint()))
    assert response.status_code == 503


def test_api_key_does_not_authorize_assertion_mode(assertion_contract):
    client, _mint, _replay = assertion_contract
    response = client.post(
        "/v1/graph/extract", headers={"X-Api-Key": "legacy-static-key"}
    )
    assert response.status_code == 401


def test_unsigned_resource_header_does_not_authorize(assertion_contract):
    client, _mint, _replay = assertion_contract
    response = client.post(
        "/v1/graph/extract",
        headers={
            "x-captify-assertion-resource": (
                "document:tenant-a:doc-1:/v1/graph/extract"
            ),
            "x-captify-tenant-id": "tenant-a",
            "x-captify-document-id": "doc-1",
        },
    )
    assert response.status_code == 401
