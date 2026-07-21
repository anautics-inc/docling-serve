"""Post-deploy readiness and canonical conversion acceptance gate."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import uuid

import boto3
import httpx
import jwt

TERMINAL = {"success", "failure"}


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _kms_jwt(
    claims: dict[str, object],
    *,
    key_id: str,
    region: str,
) -> str:
    header = {"alg": "RS256", "kid": key_id, "typ": "JWT"}
    encoded_header = _base64url(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    )
    encoded_claims = _base64url(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{encoded_header}.{encoded_claims}".encode()
    response = boto3.client("kms", region_name=region).sign(
        KeyId=key_id,
        Message=hashlib.sha256(signing_input).digest(),
        MessageType="DIGEST",
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )
    return f"{signing_input.decode()}.{_base64url(response['Signature'])}"


def _task_id(payload: dict) -> str:
    return str(payload.get("task_id") or payload.get("taskId") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    api_key = os.getenv("DOCLING_SERVE_ACCEPTANCE_API_KEY", "")
    private_key = os.getenv(
        "DOCLING_SERVE_ACCEPTANCE_ASSERTION_PRIVATE_KEY", ""
    ).replace("\\n", "\n")
    kms_key_id = os.getenv(
        "DOCLING_SERVE_ACCEPTANCE_ASSERTION_KMS_KEY_ID",
        os.getenv("DOCLING_SERVE_ASSERTION_KMS_KEY_ID", ""),
    )
    kms_region = os.getenv(
        "DOCLING_SERVE_ACCEPTANCE_ASSERTION_KMS_REGION",
        os.getenv("DOCLING_SERVE_ASSERTION_KMS_REGION", ""),
    )
    tenant_id = os.getenv("DOCLING_SERVE_ACCEPTANCE_TENANT_ID", "acceptance")
    document_id = os.getenv(
        "DOCLING_SERVE_ACCEPTANCE_DOCUMENT_ID", "docling-acceptance"
    )
    headers = {
        "x-captify-tenant-id": tenant_id,
        "x-tenant-id": tenant_id,
        "x-captify-document-id": document_id,
    }
    if api_key:
        headers["x-api-key"] = api_key

    def request_headers(method: str, path: str) -> dict[str, str]:
        result = dict(headers)
        if private_key or kms_key_id:
            now = int(time.time())
            claims = {
                "iss": os.getenv(
                    "DOCLING_SERVE_ACCEPTANCE_ASSERTION_ISSUER",
                    "captify-pytology",
                ),
                "aud": os.getenv(
                    "DOCLING_SERVE_ACCEPTANCE_ASSERTION_AUDIENCE",
                    "docling-service",
                ),
                "sub": "docling-acceptance",
                "tid": tenant_id,
                "cid": os.getenv(
                    "DOCLING_SERVE_ACCEPTANCE_ASSERTION_CLIENT_ID",
                    "captify-platform",
                ),
                "res": f"document:{tenant_id}:{document_id}:{path}",
                "act": method.lower(),
                "iat": now,
                "nbf": now,
                "exp": now + 120,
                "jti": uuid.uuid4().hex,
            }
            assertion = (
                jwt.encode(claims, private_key, algorithm="RS256")
                if private_key
                else _kms_jwt(
                    claims,
                    key_id=kms_key_id,
                    region=kms_region,
                )
            )
            result["x-captify-identity-assertion"] = assertion
        return result

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        timeout=30,
    ) as client:
        client.get("/ready").raise_for_status()
        adapters = client.get("/ready/adapters")
        adapters.raise_for_status()
        if not adapters.json().get("adapters", {}).get("document"):
            raise RuntimeError("document adapter is not ready")
        submitted = client.post(
            "/v1/chunk/hybrid/file/async",
            headers=request_headers("post", "/v1/chunk/hybrid/file/async"),
            files={"files": ("acceptance.md", b"# Acceptance\n\nDocling is ready.")},
            data={"canonical": "true", "profile": "document"},
        )
        submitted.raise_for_status()
        task_id = _task_id(submitted.json())
        if not task_id:
            raise RuntimeError("canonical submission returned no task id")
        deadline = time.monotonic() + args.timeout
        status = ""
        while time.monotonic() < deadline:
            status_path = f"/v1/status/poll/{task_id}"
            polled = client.get(
                status_path,
                headers=request_headers("get", status_path),
            )
            polled.raise_for_status()
            body = polled.json()
            status = str(
                body.get("task_status") or body.get("taskStatus") or ""
            ).lower()
            if status in TERMINAL:
                break
            time.sleep(1)
        if status != "success":
            raise RuntimeError(
                f"canonical acceptance task ended with {status or 'timeout'}"
            )
        result_path = f"/v1/result/{task_id}"
        result = client.get(
            result_path,
            headers=request_headers("get", result_path),
        )
        result.raise_for_status()
        if "docling.canonical-ingestion.v1" not in result.text:
            raise RuntimeError("canonical result contract is absent")
        client.get(
            "/v1/clear/results",
            params={"older_then": 0},
            headers=request_headers("get", "/v1/clear/results"),
        ).raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
