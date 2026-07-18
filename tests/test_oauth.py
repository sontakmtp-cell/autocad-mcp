"""Phase 4 OIDC token verification tests."""

from __future__ import annotations

import base64
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from autocad_mcp.config import TransportConfig
from autocad_mcp.oauth import OIDCTokenVerifier, create_oauth_runtime


def _b64url(value: int) -> str:
    size = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode()


def _key_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "phase4-test-key",
        "use": "sig",
        "alg": "RS256",
        "n": _b64url(public_numbers.n),
        "e": _b64url(public_numbers.e),
    }
    return private_key, jwk


@pytest.fixture
def token_material():
    private_key, jwk = _key_material()
    issuer = "https://issuer.example"
    audience = "https://autocad.example"

    def make_token(**overrides):
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": "chatgpt-user",
            "exp": int(time.time()) + 300,
            "scope": "autocad.read",
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

    return issuer, audience, jwk, make_token


@pytest.mark.asyncio
async def test_valid_oidc_token_is_normalized(token_material):
    issuer, audience, jwk, make_token = token_material
    metadata = {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/jwks",
    }

    async def fetch_json(url):
        return metadata if url.endswith("openid-configuration") else {"keys": [jwk]}

    verifier = OIDCTokenVerifier(
        issuer=issuer,
        audience=audience,
        fetch_json=fetch_json,
    )
    result = await verifier.verify_token(make_token(scope="autocad.read autocad.write"))

    assert result is not None
    assert result.client_id == "chatgpt-user"
    assert result.scopes == ["autocad.read", "autocad.write"]


@pytest.mark.asyncio
async def test_auth0_permissions_claim_is_normalized(token_material):
    issuer, audience, jwk, make_token = token_material

    async def fetch_json(url):
        if url.endswith("openid-configuration"):
            return {"issuer": issuer, "jwks_uri": f"{issuer}/jwks"}
        return {"keys": [jwk]}

    verifier = OIDCTokenVerifier(
        issuer=issuer,
        audience=audience,
        fetch_json=fetch_json,
    )
    result = await verifier.verify_token(
        make_token(scope="openid profile", permissions=["autocad.read", "autocad.write"])
    )

    assert result is not None
    assert result.scopes == ["openid", "profile", "autocad.read", "autocad.write"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://other.example"},
        {"aud": "other-resource"},
        {"exp": int(time.time()) - 1},
    ],
)
async def test_invalid_oidc_claims_are_rejected(token_material, claims):
    issuer, audience, jwk, make_token = token_material

    async def fetch_json(url):
        if url.endswith("openid-configuration"):
            return {"issuer": issuer, "jwks_uri": f"{issuer}/jwks"}
        return {"keys": [jwk]}

    verifier = OIDCTokenVerifier(
        issuer=issuer,
        audience=audience,
        fetch_json=fetch_json,
    )
    result = await verifier.verify_token(make_token(**claims))

    assert result is None


@pytest.mark.asyncio
async def test_discovery_issuer_mismatch_is_rejected(token_material):
    issuer, audience, jwk, make_token = token_material

    async def fetch_json(url):
        if url.endswith("openid-configuration"):
            return {
                "issuer": "https://attacker.example",
                "jwks_uri": f"{issuer}/jwks",
            }
        return {"keys": [jwk]}

    verifier = OIDCTokenVerifier(
        issuer=issuer,
        audience=audience,
        fetch_json=fetch_json,
    )

    assert await verifier.verify_token(make_token()) is None


def test_oauth_runtime_requires_read_scope_at_http_boundary():
    runtime = create_oauth_runtime(
        TransportConfig(
            transport="streamable-http",
            remote_profile="production",
            auth_mode="oauth",
            public_base_url="https://autocad.example",
            oauth_issuer="https://issuer.example",
            oauth_audience="https://autocad.example",
        )
    )

    assert runtime.auth_settings.required_scopes == ["autocad.read"]
