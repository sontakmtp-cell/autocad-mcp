"""Unit tests for Phase 2 remote policy, path, startup, and image guards."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from autocad_mcp import client
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.config import TransportConfig
from autocad_mcp.remote_policy import (
    PolicyDecision,
    check_path_allowed,
    evaluate_operation,
    host_is_allowed,
    validate_remote_startup,
)


def _dev_config(**overrides) -> TransportConfig:
    values = {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/mcp",
        "remote_profile": "dev",
        "allow_no_auth": True,
    }
    values.update(overrides)
    return TransportConfig(**values)


@pytest.mark.parametrize(
    ("tool", "operation"),
    [
        ("system", "health"),
        ("system", "status"),
        ("system", "get_backend"),
        ("system", "tool_manifest"),
        ("drawing", "info"),
        ("entity", "list"),
        ("entity", "count"),
        ("entity", "get"),
        ("layer", "list"),
        ("block", "list"),
        ("view", "get_screenshot"),
    ],
)
def test_no_auth_safe_allowlist_allows_read_operations(tool, operation):
    decision = evaluate_operation(
        tool=tool,
        operation=operation,
        data=None,
        config=_dev_config(),
    )

    assert decision == PolicyDecision.allow()


@pytest.mark.parametrize(
    ("tool", "operation", "expected_code"),
    [
        ("system", "runtime", "operation_not_allowlisted"),
        ("system", "init", "operation_not_allowlisted"),
        ("system", "execute_lisp", "execute_lisp_denied"),
        ("drawing", "create", "operation_not_allowlisted"),
        ("drawing", "open", "operation_not_allowlisted"),
        ("drawing", "save", "operation_not_allowlisted"),
        ("entity", "create_line", "operation_not_allowlisted"),
        ("entity", "erase", "operation_not_allowlisted"),
        ("view", "zoom_extents", "operation_not_allowlisted"),
    ],
)
def test_no_auth_safe_allowlist_denies_writes_and_unlisted_operations(
    tool, operation, expected_code
):
    decision = evaluate_operation(
        tool=tool,
        operation=operation,
        data=None,
        config=_dev_config(),
    )

    assert decision.allowed is False
    assert decision.code == expected_code


def test_execute_lisp_is_denied_in_any_remote_profile():
    decision = evaluate_operation(
        tool="system",
        operation="execute_lisp",
        data={"code": "(+ 1 2)"},
        config=_dev_config(
            remote_profile="production",
            auth_mode="oauth",
            oauth_issuer="https://issuer.example",
            oauth_audience="autocad",
            allowed_hosts=("example.com",),
        ),
    )

    assert decision.allowed is False
    assert decision.code == "execute_lisp_denied"


def test_stdio_profile_is_not_restricted_by_remote_allowlist():
    config = TransportConfig(transport="stdio")

    decision = evaluate_operation(
        tool="entity",
        operation="create_line",
        data=None,
        config=config,
    )

    assert decision == PolicyDecision.allow()


def test_remote_startup_requires_explicit_dev_no_auth():
    with pytest.raises(RuntimeError, match="REMOTE_PROFILE=dev"):
        validate_remote_startup(
            _dev_config(remote_profile="off", allow_no_auth=False)
        )

    with pytest.raises(RuntimeError, match="No Authentication is fail-closed"):
        validate_remote_startup(_dev_config(allow_no_auth=False))


def test_remote_startup_accepts_configured_oauth():
    with pytest.raises(RuntimeError, match="OAUTH_ISSUER"):
        validate_remote_startup(
            _dev_config(remote_profile="production", auth_mode="oauth")
        )

    assert validate_remote_startup(
        _dev_config(
            remote_profile="production",
            auth_mode="oauth",
            oauth_issuer="https://issuer.example",
            oauth_audience="https://example.com",
            public_base_url="https://example.com",
            allowed_hosts=("example.com",),
        )
    ) is None


def test_production_oauth_requires_public_resource_url():
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        validate_remote_startup(
            _dev_config(
                remote_profile="production",
                auth_mode="oauth",
                oauth_issuer="https://issuer.example",
                oauth_audience="autocad",
                allowed_hosts=("example.com",),
            )
        )


@pytest.mark.parametrize(
    ("tool", "operation", "scopes", "expected_code"),
    [
        ("drawing", "info", ("autocad.read",), "allow"),
        ("entity", "create_line", ("autocad.read",), "scope_missing"),
        ("entity", "create_line", ("autocad.read", "autocad.write"), "allow"),
        ("annotation", "detect_parts", ("autocad.read",), "allow"),
        ("annotation", "plan_dimensions", ("autocad.read",), "allow"),
        ("annotation", "audit_dimensions", ("autocad.read",), "allow"),
        ("system", "tool_manifest", ("autocad.read",), "allow"),
        ("annotation", "commit_dimension_plan", ("autocad.read",), "scope_missing"),
        (
            "annotation",
            "commit_dimension_plan",
            ("autocad.read", "autocad.write"),
            "allow",
        ),
        ("system", "execute_lisp", ("autocad.read", "autocad.write"), "execute_lisp_denied"),
        ("drawing", "info", (), "scope_missing"),
    ],
)
def test_oauth_scope_policy(tool, operation, scopes, expected_code):
    decision = evaluate_operation(
        tool=tool,
        operation=operation,
        data=None,
        scopes=scopes,
        config=_dev_config(
            remote_profile="production",
            auth_mode="oauth",
            oauth_issuer="https://issuer.example",
            oauth_audience="https://example.com",
            public_base_url="https://example.com",
            allowed_hosts=("example.com",),
        ),
    )

    assert decision.code == expected_code
    assert decision.allowed is (expected_code == "allow")


def test_remote_startup_requires_https_public_url():
    with pytest.raises(RuntimeError, match="must use HTTPS"):
        validate_remote_startup(
            _dev_config(
                public_base_url="http://example.com/mcp",
                allowed_hosts=("example.com",),
            )
        )


def test_remote_startup_requires_allowed_hosts_for_public_url():
    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        validate_remote_startup(
            _dev_config(public_base_url="https://example.com/mcp")
        )


def test_host_allowlist_compares_hostname_only():
    assert host_is_allowed("example.com:8765", ("example.com",))
    assert host_is_allowed("EXAMPLE.COM.", ("example.com",))
    assert not host_is_allowed("other.example.com", ("example.com",))


def test_path_guard_allows_matching_directory_and_extension(tmp_path: Path):
    allowed_dir = tmp_path / "drawings"
    allowed_dir.mkdir()
    config = _dev_config(allowed_dirs=(str(allowed_dir),))

    decision = check_path_allowed(
        operation="open",
        path=str(allowed_dir / "sample.dxf"),
        config=config,
    )

    assert decision == PolicyDecision.allow()


@pytest.mark.parametrize(
    ("operation", "path", "code"),
    [
        ("open", None, "path_missing"),
        ("open", "", "path_missing"),
        ("open", r"\\server\share\sample.dxf", "path_unc"),
        ("open", r"\\.\NUL", "path_device_namespace"),
        ("open", r"C:\drawings\sample.dxf:secret", "path_ads"),
        ("open", r"C:\drawings\..\outside.dxf", "path_traversal"),
    ],
)
def test_path_guard_rejects_unsafe_windows_forms(operation, path, code):
    decision = check_path_allowed(
        operation=operation,
        path=path,
        config=_dev_config(allowed_dirs=(r"C:\drawings",)),
    )

    assert decision.allowed is False
    assert decision.code == code


def test_path_guard_fails_closed_for_empty_allowlist(tmp_path: Path):
    decision = check_path_allowed(
        operation="open",
        path=str(tmp_path / "sample.dxf"),
        config=_dev_config(),
    )

    assert decision.code == "path_allowlist_empty"


def test_path_guard_rejects_outside_and_wrong_extension(tmp_path: Path):
    allowed_dir = tmp_path / "drawings"
    outside_dir = tmp_path / "outside"
    allowed_dir.mkdir()
    outside_dir.mkdir()
    config = _dev_config(allowed_dirs=(str(allowed_dir),))

    outside = check_path_allowed(
        operation="open",
        path=str(outside_dir / "sample.dxf"),
        config=config,
    )
    wrong_extension = check_path_allowed(
        operation="save_as_dxf",
        path=str(allowed_dir / "sample.dwg"),
        config=config,
    )

    assert outside.code == "path_outside_allowlist"
    assert wrong_extension.code == "path_extension_not_allowed"


def test_path_guard_rejects_symlink_escape_when_supported(tmp_path: Path):
    allowed_dir = tmp_path / "drawings"
    outside_dir = tmp_path / "outside"
    allowed_dir.mkdir()
    outside_dir.mkdir()
    link = allowed_dir / "link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is not available in this Windows environment")

    decision = check_path_allowed(
        operation="open",
        path=str(link / "sample.dxf"),
        config=_dev_config(allowed_dirs=(str(allowed_dir),)),
    )

    assert decision.code == "path_outside_allowlist"


@pytest.mark.asyncio
async def test_remote_screenshot_size_guard(monkeypatch):
    class FakeBackend:
        async def get_screenshot(self):
            return CommandResult(
                ok=True,
                payload=base64.b64encode(b"0123456789").decode("ascii"),
            )

    monkeypatch.setenv("AUTOCAD_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AUTOCAD_MCP_REMOTE_PROFILE", "dev")
    monkeypatch.setenv("AUTOCAD_MCP_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("AUTOCAD_MCP_MAX_IMAGE_BYTES", "5")
    monkeypatch.setattr(client, "get_backend", lambda: _fake_backend(FakeBackend()))

    result = await client.add_screenshot_if_available(
        CommandResult(ok=True, payload={"entity": "LINE"}),
        include_screenshot=True,
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "size limit" in payload["error"]
    assert "0123456789" not in result


@pytest.mark.asyncio
async def test_view_screenshot_size_guard(monkeypatch):
    class FakeBackend:
        name = "fake"

        async def get_screenshot(self):
            return CommandResult(
                ok=True,
                payload=base64.b64encode(b"0123456789").decode("ascii"),
            )

    monkeypatch.setenv("AUTOCAD_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AUTOCAD_MCP_REMOTE_PROFILE", "dev")
    monkeypatch.setenv("AUTOCAD_MCP_AUTH_MODE", "none")
    monkeypatch.setenv("AUTOCAD_MCP_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("AUTOCAD_MCP_MAX_IMAGE_BYTES", "5")
    monkeypatch.setattr(client, "_backend", FakeBackend())

    from autocad_mcp.server import mcp

    result = await mcp._tool_manager._tools["view"].fn(
        operation="get_screenshot"
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert "size limit" in payload["error"]
    assert "0123456789" not in result


async def _fake_backend(backend):
    return backend
