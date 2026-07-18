"""Local-only Streamable HTTP entrypoint for the MCP server."""

from __future__ import annotations

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from autocad_mcp.config import HTTP_HOST_DEFAULT, TransportConfig, load_transport_config
from autocad_mcp.remote_policy import host_is_allowed, validate_remote_startup
from autocad_mcp.server import mcp


class AllowedHostMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host is outside the configured allowlist."""

    def __init__(self, app, *, allowed_hosts: tuple[str, ...]):
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next) -> Response:
        if not host_is_allowed(request.headers.get("host", ""), self.allowed_hosts):
            return JSONResponse({"error": "Host is not allowed."}, status_code=403)
        return await call_next(request)


def create_app(config: TransportConfig | None = None) -> Starlette:
    """Create the MCP Streamable HTTP ASGI app.

    Phase 1 deliberately accepts only loopback binding. The MCP instance is
    configured when ``server`` is imported, before this factory is called.
    """

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

    app = mcp.streamable_http_app()
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
