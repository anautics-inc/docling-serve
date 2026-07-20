from __future__ import annotations

import base64
import math
import time
from functools import lru_cache
from typing import Any, Protocol, cast

import jwt
from fastapi import HTTPException, Request, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

IDENTITY_ASSERTION_HEADER = "x-captify-identity-assertion"
TENANT_HEADER = "x-captify-tenant-id"
DOCUMENT_ID_HEADER = "x-captify-document-id"
MAX_ASSERTION_TTL_SECONDS = 300
_ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)


class AuthenticationResult(BaseModel):
    valid: bool
    errors: list[str] = []
    detail: Any | None = None


class APIKeyAuth(APIKeyHeader):
    """
    FastAPI dependency which evaluates a status API Key.

    Fails CLOSED by default when no key is configured: an unset
    ``DOCLING_SERVE_API_KEY`` is a misconfiguration in any deployment reachable
    beyond localhost, and silently accepting every request is the wrong
    default for that case. Set ``allow_no_auth=True`` (``DOCLING_SERVE_ALLOW_NO_AUTH``)
    to explicitly opt back into the permissive dev/test behavior — every
    request accepted, no header required — for a deliberately unauthenticated
    local instance.
    """

    def __init__(
        self,
        api_key: str,
        header_name: str = "X-Api-Key",
        fail_on_unauthorized: bool = True,
        allow_no_auth: bool = False,
    ) -> None:
        self.api_key = api_key
        self.header_name = header_name
        self.allow_no_auth = allow_no_auth
        super().__init__(name=self.header_name, auto_error=False)

    async def _validate_api_key(self, header_api_key: str | None):
        if header_api_key is None:
            return AuthenticationResult(
                valid=False, errors=[f"Missing header {self.header_name}."]
            )

        header_api_key = header_api_key.strip()

        if header_api_key == self.api_key:
            return AuthenticationResult(
                valid=True,
                detail=header_api_key,
            )
        else:
            return AuthenticationResult(
                valid=False,
                errors=["The provided API Key is invalid."],
            )

    async def __call__(self, request: Request) -> AuthenticationResult:  # type: ignore
        header_api_key = await super().__call__(request=request)

        if not self.api_key:
            # No key configured. This is the ONLY place that decides whether
            # that means "deliberately open" or "refuse everything" — every
            # caller of this dependency gets the same posture, so there is no
            # code path that can end up silently unauthenticated by accident.
            if self.allow_no_auth:
                return AuthenticationResult(valid=True, detail=header_api_key)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Server has no API key configured; refusing to serve "
                    "requests unauthenticated. Set DOCLING_SERVE_API_KEY, or "
                    "set DOCLING_SERVE_ALLOW_NO_AUTH=true for a deliberately "
                    "unauthenticated dev/test instance."
                ),
            )

        result = await self._validate_api_key(header_api_key)
        if not result.valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=result.detail
            )
        return result


class ReplayStore(Protocol):
    def consume(self, jti: str, ttl_seconds: int) -> bool: ...


class RedisReplayStore:
    """Cross-host, atomic single-use assertion replay protection."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self._redis_url,
                socket_timeout=0.25,
                socket_connect_timeout=0.25,
            )
        return self._client

    def consume(self, jti: str, ttl_seconds: int) -> bool:
        return bool(
            self._get_client().set(
                f"captify:assertion-jti:{jti}",
                "1",
                nx=True,
                ex=max(1, ttl_seconds),
            )
        )

    def ping(self) -> None:
        if not self._get_client().ping():
            raise RuntimeError("assertion replay Redis did not answer PING")


@lru_cache(maxsize=8)
def _kms_public_key_pem(key_id: str, region: str | None) -> str:
    import boto3

    response = boto3.client("kms", region_name=region or None).get_public_key(
        KeyId=key_id
    )
    public_key = response.get("PublicKey")
    if not public_key:
        raise RuntimeError("KMS did not return assertion public key material")
    encoded = base64.b64encode(bytes(public_key)).decode("ascii")
    lines = [encoded[index : index + 64] for index in range(0, len(encoded), 64)]
    return (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(lines)
        + "\n-----END PUBLIC KEY-----\n"
    )


def _nonempty_string(payload: dict[str, Any], claim: str) -> str | None:
    value = payload.get(claim)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _valid_binding_component(value: str) -> bool:
    return (
        bool(value)
        and ":" not in value
        and not any(character.isspace() for character in value)
    )


class MachineAssertionAuth:
    """Verify one asymmetric, request-bound, single-use Captify assertion."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        client_id: str,
        algorithm: str,
        public_key: str = "",
        kms_key_id: str = "",
        kms_region: str = "",
        redis_url: str = "",
        request_tenant_header: str = "X-Tenant-Id",
        replay_store: ReplayStore | None = None,
    ) -> None:
        self.issuer = issuer.strip()
        self.audience = audience.strip()
        self.client_id = client_id.strip()
        self.algorithm = algorithm.strip().upper()
        self.public_key = public_key.replace("\\n", "\n").strip()
        self.kms_key_id = kms_key_id.strip()
        self.kms_region = kms_region.strip()
        self.redis_url = redis_url.strip()
        self.request_tenant_header = request_tenant_header.strip()
        self.replay_store = replay_store or (
            RedisReplayStore(self.redis_url) if self.redis_url else None
        )

    def _verification_key(self) -> str:
        if self.algorithm not in _ASYMMETRIC_ALGORITHMS:
            raise RuntimeError("assertion algorithm must be asymmetric")
        if self.kms_key_id:
            return _kms_public_key_pem(self.kms_key_id, self.kms_region or None)
        if self.public_key:
            return self.public_key
        raise RuntimeError("assertion public key is not configured")

    def _replay_store(self) -> ReplayStore:
        if self.replay_store is not None:
            return self.replay_store
        raise RuntimeError("assertion replay Redis is not configured")

    def check_ready(self) -> None:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        load_pem_public_key(self._verification_key().encode("utf-8"))
        replay_store = self._replay_store()
        ping = getattr(replay_store, "ping", None)
        if callable(ping):
            ping()

    async def __call__(self, request: Request) -> AuthenticationResult:
        return self.authenticate(
            method=request.method,
            path=request.url.path,
            headers=request.headers,
        )

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        headers: Any,
    ) -> AuthenticationResult:
        token = headers.get(IDENTITY_ASSERTION_HEADER)
        tenant = (headers.get(TENANT_HEADER) or "").strip()
        request_tenant = (headers.get(self.request_tenant_header) or "").strip()
        document_id = (headers.get(DOCUMENT_ID_HEADER) or "").strip()
        if (
            not token
            or request_tenant != tenant
            or not _valid_binding_component(tenant)
            or not _valid_binding_component(document_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A request-bound machine assertion is required.",
            )
        if not self.issuer or not self.audience or not self.client_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Machine assertion verifier is not configured.",
            )

        try:
            key = self._verification_key()
            payload = jwt.decode(
                token,
                key,
                algorithms=[self.algorithm],
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "tid",
                        "cid",
                        "res",
                        "act",
                        "iat",
                        "nbf",
                        "exp",
                        "jti",
                    ]
                },
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Machine assertion verifier is not configured.",
            ) from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid machine assertion.",
            ) from exc

        subject = _nonempty_string(payload, "sub")
        signed_tenant = _nonempty_string(payload, "tid")
        signed_client = _nonempty_string(payload, "cid")
        signed_resource = _nonempty_string(payload, "res")
        signed_action = _nonempty_string(payload, "act")
        jti = _nonempty_string(payload, "jti")
        timestamp_values = tuple(payload.get(claim) for claim in ("iat", "nbf", "exp"))
        if not all(
            (subject, signed_tenant, signed_client, signed_resource, signed_action, jti)
        ) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in timestamp_values
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid machine assertion claims.",
            )
        assert jti is not None
        numeric_timestamps = cast(
            tuple[int | float, int | float, int | float], timestamp_values
        )
        iat, nbf, exp = (float(value) for value in numeric_timestamps)
        if not (iat <= nbf < exp and exp - iat <= MAX_ASSERTION_TTL_SECONDS):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid machine assertion lifetime.",
            )

        expected_action = method.lower()
        expected_resource = f"document:{tenant}:{document_id}:{path}"
        if (
            signed_tenant != tenant
            or signed_client != self.client_id
            or signed_action != expected_action
            or signed_resource != expected_resource
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Machine assertion scope does not match the request.",
            )

        try:
            unseen = self._replay_store().consume(
                jti,
                max(1, math.ceil(exp - time.time())),
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Machine assertion replay protection is unavailable.",
            ) from exc
        if not unseen:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Machine assertion has already been used.",
            )

        return AuthenticationResult(valid=True, detail=payload)
