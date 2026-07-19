"""Local-only Streamable HTTP entrypoint for the MCP server."""

from __future__ import annotations

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from autocad_mcp.config import (
    HTTP_HOST_DEFAULT,
    TransportConfig,
    bind_transport_config,
    load_transport_config,
    reset_transport_config,
)
from autocad_mcp.remote_policy import host_is_allowed, validate_remote_startup
from autocad_mcp.server import mcp, register_optional_features


class AllowedHostMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host is outside the configured allowlist."""

    def __init__(self, app, *, allowed_hosts: tuple[str, ...]):
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next) -> Response:
        if not host_is_allowed(request.headers.get("host", ""), self.allowed_hosts):
            return JSONResponse({"error": "Host is not allowed."}, status_code=403)
        return await call_next(request)


class TransportConfigMiddleware:
    """Make the app's validated config available to tool handlers."""

    def __init__(self, app, *, config: TransportConfig):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        token = bind_transport_config(self.config)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_transport_config(token)


def create_app(config: TransportConfig | None = None) -> Starlette:
    """Create the MCP Streamable HTTP ASGI app.

    Phase 1 deliberately accepts only loopback binding. The MCP instance is
    configured when ``server`` is imported, before this factory is called.
    """

    register_optional_features()
    config = (config or load_transport_config()).validate()
    if config.transport != "streamable-http":
        raise RuntimeError(
            "HTTP app requires AUTOCAD_MCP_TRANSPORT=streamable-http; "
            f"got {config.transport!r}."
        )
    if config.host != HTTP_HOST_DEFAULT:
        raise RuntimeError(
            "Phase 1 Streamable HTTP is local-only and must bind to "
            f"{HTTP_HOST_DEFAULT}; got {config.host!r}."
        )
    validate_remote_startup(config)

    configured_path = getattr(mcp.settings, "streamable_http_path", None)
    if configured_path != config.path:
        raise RuntimeError(
            "MCP path was loaded before the current environment. Restart the "
            f"process after setting AUTOCAD_MCP_PATH (configured={configured_path!r}, "
            f"requested={config.path!r})."
        )
    configured_stateless = getattr(mcp.settings, "stateless_http", None)
    if configured_stateless != config.stateless_http:
        raise RuntimeError(
            "MCP session mode was loaded before the current environment. "
            "Restart the process after setting AUTOCAD_MCP_STATELESS_HTTP "
            f"(configured={configured_stateless!r}, requested={config.stateless_http!r})."
        )

    if config.auth_mode == "oauth" and getattr(mcp.settings, "auth", None) is None:
        raise RuntimeError(
            "OAuth settings were loaded before the current environment. Restart "
            "the process after setting AUTOCAD_MCP_AUTH_MODE=oauth."
        )

    if config.allowed_hosts:
        allowed_origins = []
        if config.public_base_url:
            allowed_origins.append(config.public_base_url.rstrip("/"))
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.allowed_hosts),
            allowed_origins=allowed_origins,
        )

    app = mcp.streamable_http_app()
    if config.auth_mode == "oauth":
        from autocad_mcp.oauth import protected_resource_metadata_route

        metadata_route = protected_resource_metadata_route(config)
        for index, route in enumerate(app.routes):
            if getattr(route, "path", None) == metadata_route.path:
                app.routes[index] = metadata_route
                break
        else:  # pragma: no cover - FastMCP normally creates this route for us
            app.routes.insert(0, metadata_route)

    app.add_middleware(TransportConfigMiddleware, config=config)
    if config.allowed_hosts:
        app.add_middleware(AllowedHostMiddleware, allowed_hosts=config.allowed_hosts)
    return app


def run_http_server(config: TransportConfig | None = None) -> None:
    """Run the local-only MCP HTTP server with uvicorn."""

    config = (config or load_transport_config()).validate()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,
    )


def main() -> None:
    """Standalone entrypoint: ``python -m autocad_mcp.http_server``."""

    run_http_server(load_transport_config())


if __name__ == "__main__":
    main()
