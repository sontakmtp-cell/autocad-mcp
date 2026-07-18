"""Protocol-level tests for the Phase 1 local Streamable HTTP endpoint."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from autocad_mcp.config import TransportConfig
from autocad_mcp.http_server import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_server(server: uvicorn.Server) -> None:
    for _ in range(100):
        if server.started:
            return
        if server.should_exit:
            raise RuntimeError("uvicorn exited before the test server started")
        await asyncio.sleep(0.05)
    raise TimeoutError("Timed out waiting for the HTTP test server")


@pytest_asyncio.fixture(scope="module")
async def http_endpoint():
    port = _free_port()
    config = TransportConfig(
        transport="streamable-http",
        host="127.0.0.1",
        port=port,
        path="/mcp",
        remote_profile="dev",
        allow_no_auth=True,
    )
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="error",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    await _wait_for_server(server)

    try:
        yield f"http://{config.host}:{config.port}{config.path}"
    finally:
        server.should_exit = True
        await task


def _text_content(result) -> list[str]:
    return [
        item.text
        for item in result.content
        if getattr(item, "type", None) == "text"
    ]


def _new_client(endpoint: str):
    return streamable_http_client(endpoint)


@pytest.mark.asyncio(loop_scope="module")
async def test_initialize_tools_list_and_call(http_endpoint):
    async with _new_client(http_endpoint) as (read_stream, write_stream, _):
        async with ClientSession(
            read_stream,
            write_stream,
            client_info=Implementation(name="phase1-test-client", version="1.0"),
        ) as session:
            initialize_result = await session.initialize()
            tools_result = await session.list_tools()
            call_result = await session.call_tool(
                "system", {"operation": "health"}
            )

    assert initialize_result.serverInfo.name == "autocad-mcp"
    assert {
        "drawing",
        "entity",
        "layer",
        "block",
        "annotation",
        "pid",
        "view",
        "system",
    }.issubset({tool.name for tool in tools_result.tools})
    tools_by_name = {tool.name: tool for tool in tools_result.tools}
    assert tools_by_name["system"].annotations.readOnlyHint is False
    assert tools_by_name["view"].annotations.readOnlyHint is False
    assert call_result.isError is False
    payload = json.loads(_text_content(call_result)[0])
    assert payload["ok"] is True


@pytest.mark.asyncio(loop_scope="module")
async def test_reconnect_and_session_cleanup(http_endpoint):
    async with _new_client(http_endpoint) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            first_session_id = get_session_id()

    assert first_session_id

    async with httpx.AsyncClient(timeout=5.0) as client:
        stale_response = await client.get(
            http_endpoint,
            headers={
                "Accept": "text/event-stream",
                "Mcp-Session-Id": first_session_id,
            },
        )

    assert stale_response.status_code == 404

    async with _new_client(http_endpoint) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            second_session_id = get_session_id()

    assert second_session_id
    assert second_session_id != first_session_id


@pytest.mark.asyncio(loop_scope="module")
async def test_two_concurrent_mcp_requests(http_endpoint):
    async def call_runtime():
        async with _new_client(http_endpoint) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "system", {"operation": "health"}
                )
                return result

    first, second = await asyncio.gather(call_runtime(), call_runtime())

    assert first.isError is False
    assert second.isError is False
    assert json.loads(_text_content(first)[0])["ok"] is True
    assert json.loads(_text_content(second)[0])["ok"] is True


@pytest.mark.asyncio(loop_scope="module")
async def test_remote_policy_denies_unlisted_operation(http_endpoint):
    async with _new_client(http_endpoint) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "system", {"operation": "runtime"}
            )

    payload = json.loads(_text_content(result)[0])
    assert payload["ok"] is False
    assert "not in the Phase 2 No Authentication safe allowlist" in payload["error"]


def test_http_server_rejects_non_loopback_binding():
    config = TransportConfig(
        transport="streamable-http",
        host="0.0.0.0",
        port=8765,
        path="/mcp",
        remote_profile="dev",
        allow_no_auth=True,
    )

    with pytest.raises(RuntimeError, match="local-only"):
        create_app(config)
