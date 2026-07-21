from __future__ import annotations

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

from scripts import verify_post_deploy


def test_kms_assertion_is_a_valid_rs256_jwt(monkeypatch) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class FakeKms:
        def sign(self, **kwargs):
            assert kwargs["MessageType"] == "DIGEST"
            assert kwargs["SigningAlgorithm"] == "RSASSA_PKCS1_V1_5_SHA_256"
            return {
                "Signature": private_key.sign(
                    kwargs["Message"],
                    padding.PKCS1v15(),
                    Prehashed(hashes.SHA256()),
                )
            }

    monkeypatch.setattr(
        verify_post_deploy.boto3,
        "client",
        lambda service, *, region_name: FakeKms(),
    )
    claims = {
        "iss": "captify-pytology",
        "aud": "docling-service",
        "sub": "acceptance",
        "exp": 4_102_444_800,
    }
    token = verify_post_deploy._kms_jwt(
        claims,
        key_id="test-key",
        region="us-gov-west-1",
    )

    assert token.count(".") == 2
    assert (
        jwt.decode(
            token,
            private_key.public_key(),
            algorithms=["RS256"],
            audience="docling-service",
        )["iss"]
        == "captify-pytology"
    )
