"""OIDC resource-server support for the Phase 4 remote HTTP profile."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import structlog
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from autocad_mcp.config import OAUTH_READ_SCOPE, TransportConfig

log = structlog.get_logger()

JsonFetcher = Callable[[str], Awaitable[dict[str, Any]]]
SUPPORTED_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)


def _normalized_issuer(value: str) -> str:
    return value.rstrip("/")


def _extract_scopes(claims: dict[str, Any]) -> list[str]:
    extracted: list[str] = []
    seen: set[str] = set()
    for raw_scope in (
        claims.get("scope"),
        claims.get("scp"),
        # Auth0 RBAC can emit API permissions in this claim.
        claims.get("permissions"),
    ):
        if isinstance(raw_scope, str):
            values = raw_scope.split()
        elif isinstance(raw_scope, list):
            values = [item for item in raw_scope if isinstance(item, str)]
        else:
            values = []
        for item in values:
            if item and item not in seen:
                seen.add(item)
                extracted.append(item)
    return extracted


def _audience_for_log(value: Any) -> str | list[str]:
    """Return a bounded, non-secret representation of an aud claim."""

    if isinstance(value, str):
        return value[:256]
    if isinstance(value, list):
        return [item[:256] for item in value if isinstance(item, str)][:8]
    return type(value).__name__


class OIDCTokenVerifier(TokenVerifier):
    """Validate signed OIDC access tokens against issuer discovery and JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        fetch_json: JsonFetcher | None = None,
        cache_ttl_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer = _normalized_issuer(issuer)
        self.audience = audience
        self._fetch_json_override = fetch_json
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._discovery: dict[str, Any] | None = None
        self._discovery_expires_at = 0.0
        self._jwks: list[dict[str, Any]] | None = None
        self._jwks_expires_at = 0.0

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        if self._fetch_json_override:
            return await self._fetch_json_override(url)

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("OAuth discovery response must be a JSON object.")
        return payload

    async def _load_discovery(self) -> dict[str, Any]:
        now = self._clock()
        if self._discovery and now < self._discovery_expires_at:
            return self._discovery

        urls = (
            f"{self.issuer}/.well-known/openid-configuration",
            f"{self.issuer}/.well-known/oauth-authorization-server",
        )
        last_error: Exception | None = None
        for url in urls:
            try:
                payload = await self._fetch_json(url)
                discovered_issuer = payload.get("issuer")
                if not isinstance(discovered_issuer, str) or _normalized_issuer(
                    discovered_issuer
                ) != self.issuer:
                    raise ValueError("OAuth discovery issuer does not match configuration.")
                jwks_uri = payload.get("jwks_uri")
                if not isinstance(jwks_uri, str) or not jwks_uri:
                    raise ValueError("OAuth discovery does not provide jwks_uri.")
                self._discovery = payload
                self._discovery_expires_at = now + self._cache_ttl_seconds
                return payload
            except Exception as exc:  # pragma: no cover - exact provider errors vary
                last_error = exc

        raise RuntimeError("Unable to load OAuth discovery metadata.") from last_error

    async def _load_jwks(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        now = self._clock()
        if self._jwks and not force_refresh and now < self._jwks_expires_at:
            return self._jwks

        discovery = await self._load_discovery()
        payload = await self._fetch_json(str(discovery["jwks_uri"]))
        keys = payload.get("keys")
        if not isinstance(keys, list) or not all(isinstance(item, dict) for item in keys):
            raise ValueError("OAuth JWKS response must contain a keys array.")
        self._jwks = keys
        self._jwks_expires_at = now + self._cache_ttl_seconds
        return keys

    async def _find_jwk(self, key_id: str | None) -> dict[str, Any] | None:
        for refresh in (False, True):
            keys = await self._load_jwks(force_refresh=refresh)
            matches = [item for item in keys if item.get("kid") == key_id]
            if matches:
                return matches[0]
            if key_id is None and len(keys) == 1:
                return keys[0]
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return normalized access information, or None for any invalid token."""

        if not token:
            return None
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in SUPPORTED_JWT_ALGORITHMS:
                log.warning("oauth_token_verification_failed", reason="unsupported_algorithm")
                return None

            jwk = await self._find_jwk(header.get("kid"))
            if jwk is None:
                log.warning("oauth_token_verification_failed", reason="unknown_signing_key")
                return None
            key = jwt.PyJWK.from_dict(jwk).key
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=self.audience,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_iss": False,
                },
            )
            if _normalized_issuer(str(claims.get("iss", ""))) != self.issuer:
                log.warning("oauth_token_verification_failed", reason="issuer_mismatch")
                return None

            client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub")
            if not isinstance(client_id, str) or not client_id:
                log.warning("oauth_token_verification_failed", reason="missing_client_id")
                return None
            expires_at = int(claims["exp"])
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=_extract_scopes(claims),
                expires_at=expires_at,
            )
        except jwt.InvalidAudienceError:
            try:
                unverified_claims = jwt.decode(
                    token,
                    options={"verify_signature": False, "verify_aud": False},
                )
                token_audience = _audience_for_log(unverified_claims.get("aud"))
            except (jwt.PyJWTError, TypeError, ValueError):
                token_audience = "unreadable"
            log.warning(
                "oauth_token_verification_failed",
                reason="invalid_audience",
                token_audience=token_audience,
                expected_audience=self.audience,
            )
            return None
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            # Keep token contents and claims out of logs; the exception class is
            # enough to diagnose provider/audience/signature configuration.
            log.warning(
                "oauth_token_verification_failed",
                reason=type(exc).__name__,
            )
            return None


@dataclass(frozen=True)
class OAuthRuntime:
    verifier: OIDCTokenVerifier
    auth_settings: AuthSettings


def create_oauth_runtime(config: TransportConfig) -> OAuthRuntime:
    """Build the Phase 4 resource-server verifier from environment config."""

    if config.auth_mode != "oauth":
        raise ValueError("OAuth runtime requires AUTOCAD_MCP_AUTH_MODE=oauth.")
    if not config.oauth_issuer or not config.oauth_audience:
        raise ValueError("OAuth runtime requires issuer and audience.")
    if not config.public_base_url:
        raise ValueError("OAuth runtime requires AUTOCAD_MCP_PUBLIC_BASE_URL.")

    issuer = _normalized_issuer(config.oauth_issuer)
    resource_server = config.public_base_url.rstrip("/")
    return OAuthRuntime(
        verifier=OIDCTokenVerifier(issuer=issuer, audience=config.oauth_audience),
        auth_settings=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource_server,
            # Every remote operation needs read access. Enforce that at the HTTP
            # auth boundary so clients receive an insufficient_scope challenge
            # and can re-authorize instead of getting a successful MCP response
            # whose tool payload merely contains an error. Operation-level policy
            # still distinguishes autocad.read from autocad.write.
            required_scopes=[OAUTH_READ_SCOPE],
        ),
    )


def protected_resource_metadata_route(config: TransportConfig):
    """Build the RFC 9728 metadata route with both read and write scopes."""

    from mcp.server.auth.routes import create_protected_resource_routes

    if not config.oauth_issuer or not config.public_base_url:
        raise ValueError("OAuth metadata requires issuer and public resource URL.")
    return create_protected_resource_routes(
        resource_url=AnyHttpUrl(config.public_base_url.rstrip("/")),
        authorization_servers=[AnyHttpUrl(_normalized_issuer(config.oauth_issuer))],
        scopes_supported=list(config.oauth_scopes),
        resource_name="AutoCAD MCP",
    )[0]
