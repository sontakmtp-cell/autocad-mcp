"""Centralized policy and path checks for remote MCP requests."""

from __future__ import annotations

import ntpath
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

from autocad_mcp.config import (
    HTTP_HOST_DEFAULT,
    OAUTH_READ_SCOPE,
    OAUTH_WRITE_SCOPE,
    TransportConfig,
)


SAFE_NO_AUTH_OPERATIONS: dict[str, frozenset[str]] = {
    "system": frozenset({"health", "status", "get_backend", "tool_manifest"}),
    "drawing": frozenset({"info"}),
    "entity": frozenset({"list", "count", "get"}),
    "layer": frozenset({"list"}),
    "block": frozenset({"list"}),
    "view": frozenset({"get_screenshot"}),
}

PATH_OPERATIONS: dict[str, frozenset[str]] = {
    "open": frozenset({".dwg", ".dxf"}),
    "save": frozenset({".dwg", ".dxf"}),
    "save_as_dxf": frozenset({".dxf"}),
    "plot_pdf": frozenset({".pdf"}),
}

OAUTH_READ_OPERATIONS: dict[str, frozenset[str]] = {
    "system": frozenset({"health", "status", "get_backend", "runtime", "tool_manifest"}),
    "drawing": frozenset({"info", "get_variables"}),
    "entity": frozenset({"list", "count", "get"}),
    "layer": frozenset({"list"}),
    "block": frozenset({"list", "get_attributes"}),
    "pid": frozenset({"list_symbols"}),
    "view": frozenset({"get_screenshot"}),
    "annotation": frozenset({"detect_parts", "plan_dimensions", "audit_dimensions"}),
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str = "allow"
    reason: str = ""

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, code: str, reason: str) -> "PolicyDecision":
        return cls(allowed=False, code=code, reason=reason)


def _is_remote_profile(config: TransportConfig) -> bool:
    return config.remote_profile != "off"


def evaluate_operation(
    *,
    tool: str,
    operation: str,
    data: dict | None,
    config: TransportConfig,
    scopes: Collection[str] | None = None,
) -> PolicyDecision:
    """Return the centralized allow/deny decision for one tool operation."""

    normalized_tool = tool.strip().lower()
    normalized_operation = operation.strip().lower()

    if not _is_remote_profile(config):
        return PolicyDecision.allow()

    if normalized_tool == "system" and normalized_operation == "execute_lisp":
        return PolicyDecision.deny(
            "execute_lisp_denied",
            "execute_lisp is permanently disabled for every remote profile.",
        )

    if config.auth_mode == "oauth":
        granted_scopes = set(scopes or ())
        if OAUTH_READ_SCOPE not in granted_scopes:
            return PolicyDecision.deny(
                "scope_missing",
                f"OAuth token requires the {OAUTH_READ_SCOPE} scope.",
            )
        if normalized_operation not in OAUTH_READ_OPERATIONS.get(
            normalized_tool, frozenset()
        ) and OAUTH_WRITE_SCOPE not in granted_scopes:
            return PolicyDecision.deny(
                "scope_missing",
                f"OAuth token requires the {OAUTH_WRITE_SCOPE} scope for this operation.",
            )
    elif config.auth_mode != "none":
        return PolicyDecision.deny(
            "auth_not_ready",
            "Unsupported remote authentication mode.",
        )

    if config.auth_mode == "none":
        if config.remote_profile != "dev" or not config.allow_no_auth:
            return PolicyDecision.deny(
                "no_auth_not_explicitly_enabled",
                "No Authentication requires REMOTE_PROFILE=dev and "
                "AUTOCAD_MCP_ALLOW_NO_AUTH=1.",
            )

        allowed_operations = SAFE_NO_AUTH_OPERATIONS.get(normalized_tool, frozenset())
        if normalized_operation not in allowed_operations:
            return PolicyDecision.deny(
                "operation_not_allowlisted",
                f"{normalized_tool}.{normalized_operation} is not in the Phase 2 "
                "No Authentication safe allowlist.",
            )

    if normalized_tool == "drawing" and normalized_operation in PATH_OPERATIONS:
        path_decision = check_path_allowed(
            operation=normalized_operation,
            path=(data or {}).get("path"),
            config=config,
        )
        if not path_decision.allowed:
            return path_decision

    return PolicyDecision.allow()


def _reject_windows_path(raw_path: str) -> PolicyDecision | None:
    """Reject path forms that can escape normal Windows file semantics."""

    if "\x00" in raw_path:
        return PolicyDecision.deny("path_invalid", "Path contains a NUL character.")

    windows_path = raw_path.replace("/", "\\")
    if windows_path.startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        return PolicyDecision.deny(
            "path_device_namespace",
            "Device and extended-length Windows paths are not allowed remotely.",
        )
    if windows_path.startswith("\\\\"):
        return PolicyDecision.deny(
            "path_unc",
            "UNC and network-share paths are not allowed remotely.",
        )

    drive, tail = ntpath.splitdrive(windows_path)
    if drive.startswith("\\\\"):
        return PolicyDecision.deny(
            "path_unc",
            "UNC and network-share paths are not allowed remotely.",
        )
    if ":" in tail:
        return PolicyDecision.deny(
            "path_ads",
            "Alternate data stream syntax is not allowed remotely.",
        )

    if ".." in PureWindowsPath(windows_path).parts:
        return PolicyDecision.deny(
            "path_traversal",
            "Parent-directory traversal is not allowed remotely.",
        )

    return None


def _canonical_path(path: Path) -> str:
    value = os.path.normcase(str(path))
    if len(value) > 3:
        value = value.rstrip("\\/")
    return value


def _is_within(candidate: str, root: str) -> bool:
    if candidate == root:
        return True
    root_prefix = root.rstrip("\\/") + os.sep
    return candidate.startswith(root_prefix)


def check_path_allowed(
    *,
    operation: str,
    path: object,
    config: TransportConfig,
) -> PolicyDecision:
    """Apply fail-closed directory, extension, and Windows path checks."""

    allowed_extensions = PATH_OPERATIONS.get(operation)
    if allowed_extensions is None:
        return PolicyDecision.deny(
            "path_operation_not_supported",
            f"Path guard does not recognize drawing.{operation}.",
        )
    if not config.allowed_dirs:
        return PolicyDecision.deny(
            "path_allowlist_empty",
            "Remote path operations require AUTOCAD_MCP_ALLOWED_DIRS.",
        )
    if path is None or not str(path).strip():
        return PolicyDecision.deny(
            "path_missing",
            f"drawing.{operation} requires an explicit path in remote mode.",
        )

    raw_path = str(path).strip()
    rejected = _reject_windows_path(raw_path)
    if rejected is not None:
        return rejected

    try:
        candidate_path = Path(raw_path).expanduser().resolve(strict=False)
        allowed_roots = [
            Path(root).expanduser().resolve(strict=False)
            for root in config.allowed_dirs
        ]
    except (OSError, RuntimeError, ValueError) as exc:
        return PolicyDecision.deny(
            "path_normalization_failed",
            f"Could not safely normalize the requested path: {exc}",
        )

    candidate = _canonical_path(candidate_path)
    if not any(_is_within(candidate, _canonical_path(root)) for root in allowed_roots):
        return PolicyDecision.deny(
            "path_outside_allowlist",
            "Requested path is outside AUTOCAD_MCP_ALLOWED_DIRS.",
        )

    extension = candidate_path.suffix.lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        return PolicyDecision.deny(
            "path_extension_not_allowed",
            f"drawing.{operation} only permits: {allowed}.",
        )

    return PolicyDecision.allow()


def _is_local_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def validate_remote_startup(config: TransportConfig) -> None:
    """Fail closed before starting an HTTP server with unsafe remote config."""

    if config.transport != "streamable-http":
        return
    if config.host != HTTP_HOST_DEFAULT:
        raise RuntimeError(
            "Phase 2 remote HTTP must bind to 127.0.0.1; public binding is forbidden."
        )
    if config.remote_profile == "off":
        raise RuntimeError(
            "HTTP remote mode requires AUTOCAD_MCP_REMOTE_PROFILE=dev or production."
        )
    if config.auth_mode == "none":
        if config.remote_profile != "dev" or not config.allow_no_auth:
            raise RuntimeError(
                "No Authentication is fail-closed: set "
                "AUTOCAD_MCP_REMOTE_PROFILE=dev and "
                "AUTOCAD_MCP_ALLOW_NO_AUTH=1 for a short-lived demo."
            )
    elif config.auth_mode == "oauth":
        if not config.oauth_issuer or not config.oauth_audience:
            raise RuntimeError(
                "OAuth mode requires AUTOCAD_MCP_OAUTH_ISSUER and "
                "AUTOCAD_MCP_OAUTH_AUDIENCE."
            )
        issuer = urlparse(config.oauth_issuer)
        if issuer.scheme != "https" and not _is_local_url(config.oauth_issuer):
            raise RuntimeError("AUTOCAD_MCP_OAUTH_ISSUER must use HTTPS remotely.")
        if issuer.query or issuer.fragment:
            raise RuntimeError("AUTOCAD_MCP_OAUTH_ISSUER must not contain query or fragment.")

    if config.remote_profile == "production":
        if config.auth_mode != "oauth":
            raise RuntimeError("Production remote profile requires OAuth.")
        if not config.public_base_url:
            raise RuntimeError("Production remote profile requires AUTOCAD_MCP_PUBLIC_BASE_URL.")
        if not _is_local_url(config.public_base_url) and not config.allowed_hosts:
            raise RuntimeError(
                "Production remote profile requires AUTOCAD_MCP_ALLOWED_HOSTS."
            )

    if config.public_base_url:
        parsed = urlparse(config.public_base_url)
        if parsed.scheme != "https" and not _is_local_url(config.public_base_url):
            raise RuntimeError("AUTOCAD_MCP_PUBLIC_BASE_URL must use HTTPS remotely.")
        if not config.allowed_hosts:
            raise RuntimeError(
                "AUTOCAD_MCP_PUBLIC_BASE_URL requires AUTOCAD_MCP_ALLOWED_HOSTS."
            )
        if parsed.hostname and parsed.hostname.lower().rstrip(".") not in {
            host.lower().rstrip(".") for host in config.allowed_hosts
        }:
            raise RuntimeError(
                "AUTOCAD_MCP_PUBLIC_BASE_URL hostname must be present in "
                "AUTOCAD_MCP_ALLOWED_HOSTS."
            )


def host_is_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Compare a request Host header without logging or exposing secrets."""

    hostname = host.strip().lower().rstrip(".")
    if ":" in hostname and not hostname.startswith("["):
        hostname = hostname.rsplit(":", 1)[0]
    if hostname.startswith("[") and "]" in hostname:
        hostname = hostname[1 : hostname.index("]")]
    return hostname in {item.lower().rstrip(".") for item in allowed_hosts}
