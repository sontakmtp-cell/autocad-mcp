"""Phase 0 spike for MCP Streamable HTTP on the pinned MCP SDK.

This is intentionally independent from the production server. It proves the
SDK API and keeps the current stdio entrypoint untouched until Phase 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.types import Implementation
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


HOST = "127.0.0.1"
PATH = "/mcp"
MIDDLEWARE_HEADER = "X-AutoCAD-MCP-Phase0"


class Phase0HeaderMiddleware(BaseHTTPMiddleware):
    """Minimal proof that Starlette middleware can wrap the MCP app."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers[MIDDLEWARE_HEADER] = "streamable-http"
        return response


def build_server(*, port: int, stateless_http: bool) -> FastMCP:
    """Build the smallest server that exercises Phase 0 SDK options."""

    server = FastMCP(
        "autocad-mcp-phase0-spike",
        instructions=(
            "Phase 0 spike only. Call probe to verify the MCP Streamable HTTP path."
        ),
        host=HOST,
        port=port,
        streamable_http_path=PATH,
        stateless_http=stateless_http,
    )

    @server.tool(
        annotations={
            "title": "Phase 0 Probe",
            "readOnlyHint": True,
            "openWorldHint": False,
        }
    )
    async def probe(message: str) -> str:
        """Return a deterministic value so a real tools/call can be checked."""

        return f"probe-ok:{message}"

    return server


def _annotation_dict(tool: Any) -> dict[str, Any] | None:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if hasattr(annotations, "model_dump"):
        return annotations.model_dump(exclude_none=True)
    return dict(annotations)


async def wait_for_server(server: uvicorn.Server) -> None:
    for _ in range(100):
        if server.started:
            return
        if server.should_exit:
            raise RuntimeError("uvicorn exited before the spike server started")
        await asyncio.sleep(0.05)
    raise TimeoutError("Timed out waiting for the spike server to start")


async def run_spike(*, port: int, stateless_http: bool) -> dict[str, Any]:
    mcp_server = build_server(port=port, stateless_http=stateless_http)
    app = mcp_server.streamable_http_app()
    app.add_middleware(Phase0HeaderMiddleware)

    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="error",
        access_log=False,
    )
    uvicorn_server = uvicorn.Server(config)
    server_task = asyncio.create_task(uvicorn_server.serve())

    endpoint = f"http://{HOST}:{port}{PATH}"
    try:
        await wait_for_server(uvicorn_server)

        async with httpx.AsyncClient() as http_client:
            middleware_response = await http_client.get(endpoint)
            middleware_value = middleware_response.headers.get(MIDDLEWARE_HEADER)

        async with streamable_http_client(endpoint) as (
            read_stream,
            write_stream,
            get_session_id,
        ):
            async with ClientSession(
                read_stream,
                write_stream,
                client_info=Implementation(name="phase0-spike-client", version="1.0"),
            ) as session:
                initialize_result = await session.initialize()
                tools_result = await session.list_tools()
                call_result = await session.call_tool(
                    "probe", {"message": "streamable-http"}
                )

        probe_text = [
            item.text
            for item in call_result.content
            if getattr(item, "type", None) == "text"
        ]
        probe_tool = next(
            tool for tool in tools_result.tools if tool.name == "probe"
        )

        return {
            "transport": "streamable-http",
            "endpoint": endpoint,
            "stateless_http": stateless_http,
            "server_instructions": mcp_server.instructions,
            "middleware_header": middleware_value,
            "session_id_after_initialize": get_session_id(),
            "server_name": initialize_result.serverInfo.name,
            "tools": [
                {
                    "name": tool.name,
                    "annotations": _annotation_dict(tool),
                }
                for tool in tools_result.tools
            ],
            "probe_tool_annotations": _annotation_dict(probe_tool),
            "probe_call_text": probe_text,
            "checks": {
                "initialize": initialize_result.serverInfo.name
                == "autocad-mcp-phase0-spike",
                "tools_list": any(tool.name == "probe" for tool in tools_result.tools),
                "tools_call": probe_text == ["probe-ok:streamable-http"],
                "middleware": middleware_value == "streamable-http",
                "instructions": bool(mcp_server.instructions),
                "annotation": _annotation_dict(probe_tool) == {
                    "title": "Phase 0 Probe",
                    "readOnlyHint": True,
                    "openWorldHint": False,
                },
            },
        }
    finally:
        uvicorn_server.should_exit = True
        await server_task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stateless", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(
        run_spike(port=args.port, stateless_http=args.stateless)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not all(result["checks"].values()):
        raise SystemExit("Phase 0 spike checks failed")


if __name__ == "__main__":
    main()
