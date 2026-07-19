"""Backend detection and environment configuration."""

from __future__ import annotations

import os
import sys
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()

# Paths. Wheels place AutoLISP resources beside this package; source checkouts
# keep them at the repository root.
_PACKAGE_LISP_DIR = Path(__file__).resolve().parent / "lisp-code"
_REPOSITORY_LISP_DIR = Path(__file__).resolve().parent.parent.parent / "lisp-code"
LISP_DIR = _PACKAGE_LISP_DIR if _PACKAGE_LISP_DIR.is_dir() else _REPOSITORY_LISP_DIR
IPC_DIR = Path(os.environ.get("AUTOCAD_MCP_IPC_DIR", "C:/temp"))

# Backend selection
BACKEND_DEFAULT = "auto"  # auto | file_ipc | ezdxf

# IPC timeout (seconds), clamped to [1, 300]
IPC_TIMEOUT = max(1.0, min(300.0, float(os.environ.get("AUTOCAD_MCP_IPC_TIMEOUT", "10.0"))))

# Screenshot
ONLY_TEXT_FEEDBACK = os.environ.get("AUTOCAD_MCP_ONLY_TEXT", "").lower() in ("1", "true", "yes")

# Win32 availability
WIN32_AVAILABLE = sys.platform == "win32"


# MCP transport configuration. The default remains stdio for compatibility
# with existing MCP clients. HTTP is intentionally local-only in Phase 1.
TRANSPORT_DEFAULT = "stdio"
HTTP_HOST_DEFAULT = "127.0.0.1"
HTTP_PORT_DEFAULT = 8765
HTTP_PATH_DEFAULT = "/mcp"
REMOTE_PROFILE_DEFAULT = "off"
AUTH_MODE_DEFAULT = "none"
MAX_IMAGE_BYTES_DEFAULT = 5 * 1024 * 1024
TRANSPORTS = frozenset({"stdio", "streamable-http", "sse"})
REMOTE_PROFILES = frozenset({"off", "dev", "production"})
AUTH_MODES = frozenset({"none", "oauth"})
OAUTH_READ_SCOPE = "autocad.read"
OAUTH_WRITE_SCOPE = "autocad.write"
OAUTH_SCOPES_DEFAULT = (OAUTH_READ_SCOPE, OAUTH_WRITE_SCOPE)


@dataclass(frozen=True)
class TransportConfig:
    """Runtime transport settings loaded from environment variables."""

    transport: str = TRANSPORT_DEFAULT
    host: str = HTTP_HOST_DEFAULT
    port: int = HTTP_PORT_DEFAULT
    path: str = HTTP_PATH_DEFAULT
    stateless_http: bool = False
    remote_profile: str = REMOTE_PROFILE_DEFAULT
    auth_mode: str = AUTH_MODE_DEFAULT
    allow_no_auth: bool = False
    allowed_dirs: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    public_base_url: str | None = None
    max_image_bytes: int = MAX_IMAGE_BYTES_DEFAULT
    oauth_issuer: str | None = None
    oauth_audience: str | None = None
    oauth_scopes: tuple[str, ...] = OAUTH_SCOPES_DEFAULT

    def validate(self) -> "TransportConfig":
        if self.transport not in TRANSPORTS:
            supported = ", ".join(sorted(TRANSPORTS))
            raise ValueError(
                f"Unsupported AUTOCAD_MCP_TRANSPORT={self.transport!r}. "
                f"Expected one of: {supported}."
            )
        if self.remote_profile not in REMOTE_PROFILES:
            supported = ", ".join(sorted(REMOTE_PROFILES))
            raise ValueError(
                f"Unsupported AUTOCAD_MCP_REMOTE_PROFILE={self.remote_profile!r}. "
                f"Expected one of: {supported}."
            )
        if self.auth_mode not in AUTH_MODES:
            supported = ", ".join(sorted(AUTH_MODES))
            raise ValueError(
                f"Unsupported AUTOCAD_MCP_AUTH_MODE={self.auth_mode!r}. "
                f"Expected one of: {supported}."
            )
        if not 1 <= self.port <= 65535:
            raise ValueError(f"AUTOCAD_MCP_PORT must be between 1 and 65535, got {self.port}.")
        if self.max_image_bytes <= 0:
            raise ValueError(
                "AUTOCAD_MCP_MAX_IMAGE_BYTES must be greater than zero, "
                f"got {self.max_image_bytes}."
            )
        if not self.path.startswith("/"):
            raise ValueError(f"AUTOCAD_MCP_PATH must start with '/', got {self.path!r}.")
        if "?" in self.path or "#" in self.path or any(char.isspace() for char in self.path):
            raise ValueError("AUTOCAD_MCP_PATH must not contain query, fragment, or whitespace.")
        return self


_active_transport_config: ContextVar[TransportConfig | None] = ContextVar(
    "autocad_mcp_active_transport_config",
    default=None,
)


def get_active_transport_config() -> TransportConfig | None:
    """Return the HTTP request config currently active in this async context."""

    return _active_transport_config.get()


def bind_transport_config(config: TransportConfig) -> Token[TransportConfig | None]:
    """Bind a transport config for the duration of one ASGI request."""

    return _active_transport_config.set(config)


def reset_transport_config(token: Token[TransportConfig | None]) -> None:
    """Restore the previous transport config after an ASGI request."""

    _active_transport_config.reset(token)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {raw!r}.")


def _normalize_http_path(raw: str) -> str:
    path = raw.strip() or HTTP_PATH_DEFAULT
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _split_env_list(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.environ.get(name, "").split(";")
        if item.strip()
    )


def _split_scope_env(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item for item in raw.replace(";", " ").replace(",", " ").split() if item)


def load_transport_config() -> TransportConfig:
    """Read and validate transport settings from the current environment."""

    config = TransportConfig(
        transport=os.environ.get("AUTOCAD_MCP_TRANSPORT", TRANSPORT_DEFAULT).strip().lower(),
        host=os.environ.get("AUTOCAD_MCP_HOST", HTTP_HOST_DEFAULT).strip(),
        port=int(os.environ.get("AUTOCAD_MCP_PORT", str(HTTP_PORT_DEFAULT))),
        path=_normalize_http_path(os.environ.get("AUTOCAD_MCP_PATH", HTTP_PATH_DEFAULT)),
        stateless_http=_env_bool("AUTOCAD_MCP_STATELESS_HTTP", False),
        remote_profile=os.environ.get(
            "AUTOCAD_MCP_REMOTE_PROFILE", REMOTE_PROFILE_DEFAULT
        ).strip().lower(),
        auth_mode=os.environ.get("AUTOCAD_MCP_AUTH_MODE", AUTH_MODE_DEFAULT)
        .strip()
        .lower(),
        allow_no_auth=_env_bool("AUTOCAD_MCP_ALLOW_NO_AUTH", False),
        allowed_dirs=_split_env_list("AUTOCAD_MCP_ALLOWED_DIRS"),
        allowed_hosts=tuple(
            host.lower().rstrip(".")
            for host in _split_env_list("AUTOCAD_MCP_ALLOWED_HOSTS")
        ),
        public_base_url=os.environ.get("AUTOCAD_MCP_PUBLIC_BASE_URL", "").strip() or None,
        max_image_bytes=int(
            os.environ.get("AUTOCAD_MCP_MAX_IMAGE_BYTES", str(MAX_IMAGE_BYTES_DEFAULT))
        ),
        oauth_issuer=os.environ.get("AUTOCAD_MCP_OAUTH_ISSUER", "").strip() or None,
        oauth_audience=os.environ.get("AUTOCAD_MCP_OAUTH_AUDIENCE", "").strip() or None,
        oauth_scopes=_split_scope_env("AUTOCAD_MCP_OAUTH_SCOPES") or OAUTH_SCOPES_DEFAULT,
    )
    return config.validate()


def _current_backend_env() -> str:
    """Read backend selection from env with normalization."""
    return os.environ.get("AUTOCAD_MCP_BACKEND", BACKEND_DEFAULT).strip().lower()


def _is_wsl() -> bool:
    """Detect WSL Linux runtime."""
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def _write_debug_snapshot(backend_env: str):
    """Optionally write backend detection debug information.

    Set AUTOCAD_MCP_DEBUG_DETECT_FILE to enable.
    """
    debug_file = os.environ.get("AUTOCAD_MCP_DEBUG_DETECT_FILE", "").strip()
    if not debug_file:
        return

    try:
        debug_path = Path(debug_file)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(f"sys.platform={sys.platform}\n")
            f.write(f"WIN32_AVAILABLE={WIN32_AVAILABLE}\n")
            f.write(f"BACKEND_ENV={backend_env}\n")
            f.write(f"python={sys.executable}\n")
    except Exception:
        # Best-effort only; never fail backend detection due debug writes.
        pass


def detect_backend() -> str:
    """Return the backend name to use: 'file_ipc' or 'ezdxf'.

    Raises RuntimeError with actionable message if explicit backend fails.
    """
    backend_env = _current_backend_env()
    _write_debug_snapshot(backend_env)

    if backend_env == "ezdxf":
        return "ezdxf"

    if backend_env in ("auto", "file_ipc"):
        if WIN32_AVAILABLE:
            try:
                from autocad_mcp.backends.file_ipc import find_autocad_window

                hwnd = find_autocad_window()
                if hwnd:
                    log.info("autocad_window_found", hwnd=hwnd)
                    return "file_ipc"
                elif backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc but no AutoCAD window found. "
                        "Start AutoCAD LT and open a .dwg file."
                    )
            except ImportError:
                if backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc requires pywin32. "
                        "Install with: pip install pywin32"
                    )
                log.info("win32_deps_missing_fallback_ezdxf")
        elif backend_env == "file_ipc":
            raise RuntimeError(
                "AUTOCAD_MCP_BACKEND=file_ipc requires Windows. "
                "Use AUTOCAD_MCP_BACKEND=ezdxf for headless mode."
            )
        elif _is_wsl():
            log.info(
                "wsl_linux_python_fallback_ezdxf",
                platform=sys.platform,
                python=sys.executable,
                hint="Launch MCP with Windows python.exe for File IPC backend.",
            )

    log.info("using_ezdxf_backend")
    return "ezdxf"
