# Phase 2 — Remote policy và guardrails

Phase 2 thêm lớp bảo vệ cho HTTP remote profile. `stdio` vẫn giữ nguyên và
không bị áp dụng allowlist remote khi `AUTOCAD_MCP_REMOTE_PROFILE=off`.

## Chạy demo local không authentication

Chỉ dùng với thời gian ngắn trên loopback; chưa được xem là production:

```powershell
$env:AUTOCAD_MCP_BACKEND="ezdxf"
$env:AUTOCAD_MCP_TRANSPORT="streamable-http"
$env:AUTOCAD_MCP_HOST="127.0.0.1"
$env:AUTOCAD_MCP_PORT="8765"
$env:AUTOCAD_MCP_PATH="/mcp"
$env:AUTOCAD_MCP_REMOTE_PROFILE="dev"
$env:AUTOCAD_MCP_AUTH_MODE="none"
$env:AUTOCAD_MCP_ALLOW_NO_AUTH="1"
$env:AUTOCAD_MCP_ALLOWED_HOSTS="127.0.0.1"

uv run python -m autocad_mcp
```

Nếu không set đồng thời `REMOTE_PROFILE=dev` và `ALLOW_NO_AUTH=1`, HTTP server
sẽ từ chối khởi động.

## Allowlist No Authentication

Chỉ các operation sau được phép:

- `system`: `health`, `status`, `get_backend`
- `drawing`: `info`
- `entity`: `list`, `count`, `get`
- `layer`: `list`
- `block`: `list`
- `view`: `get_screenshot`

`execute_lisp` luôn bị chặn trong mọi remote profile. Các thao tác tạo, sửa,
xóa, mở/lưu file và zoom không được bật trong Phase 2 No Authentication.

## Path guard

Khi một remote profile được phép xử lý path operation, server yêu cầu:

- `AUTOCAD_MCP_ALLOWED_DIRS` không rỗng;
- đường dẫn nằm trong allowlist sau khi resolve symlink/junction;
- không UNC, device path, alternate data stream hoặc `..`;
- extension đúng với operation (`.dwg`, `.dxf`, `.pdf`);
- `save` không có path bị từ chối fail-closed.

## Audit và ảnh

Mỗi tool call ghi audit an toàn vào stderr với request ID, profile, tool,
operation, allow/deny, thời lượng, backend và outcome. Không ghi token, secret,
đường dẫn raw hoặc base64.

Ảnh remote mặc định bị giới hạn `5 MB`, có thể đổi bằng
`AUTOCAD_MCP_MAX_IMAGE_BYTES`. Ảnh vượt giới hạn bị từ chối, không bị log nội
dung.

OAuth, tunnel và production profile chưa được bật; OAuth không có verifier sẽ
bị từ chối khởi động cho tới Phase 4.
