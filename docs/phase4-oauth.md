# Phase 4 — OAuth production

Phase 4 chuyển HTTP remote từ No Authentication sang OAuth 2.1/OIDC. Server này
là **resource server**: nó không tự tạo tài khoản hay trang đăng nhập. Một nhà
cung cấp OIDC bên ngoài (ví dụ Auth0, Okta hoặc Cognito) phải phát hành access
token và công bố discovery metadata cùng JWKS.

OpenAI yêu cầu MCP server công bố protected-resource metadata, authorization
server công bố OAuth/OIDC discovery, dùng PKCE và truyền `resource` xuyên suốt
luồng OAuth. Xem [hướng dẫn Authentication của Apps SDK](https://developers.openai.com/apps-sdk/build/auth).

## Scope

- `autocad.read`: đọc health, thông tin drawing, entity, layer, block, PID và screenshot.
- `autocad.write`: các operation vẽ/sửa/lưu/mở/zoom thông thường khi token đồng thời có `autocad.read`.
- `execute_lisp` mặc định bị chặn remote; chỉ bật khi set `AUTOCAD_MCP_ALLOW_EXECUTE_LISP=1`
  (script: `-AllowExecuteLisp`, wrapper: `start_mcp_chatgpt.bat`) **và** token có
  `autocad.write`. Cần backend File IPC (AutoCAD đang chạy).

## Cấu hình provider

Trong provider, tạo một OAuth/OIDC application có:

1. Authorization Code + PKCE với `S256`.
2. Access token dạng JWT có chữ ký và endpoint JWKS.
3. `aud` bằng đúng resource URL của MCP, ví dụ `https://cad.example.com`.
4. Scope `autocad.read` và `autocad.write`.
5. Redirect URI ChatGPT hiển thị trong trang quản lý app: `https://chatgpt.com/connector/oauth/{callback_id}`.

Provider phải trả về một trong hai metadata URL sau:

```text
https://issuer.example/.well-known/openid-configuration
https://issuer.example/.well-known/jwks.json
```

### Auth0 + ChatGPT DCR

Nếu ChatGPT dùng **Dynamic Client Registration (DCR)**, hãy cấu hình Auth0 theo
đúng thứ tự sau:

1. Trong API của AutoCAD MCP, đặt **Default Permissions for third-party
   applications / User-delegated Access** thành `Authorized` và chọn cả
   `autocad.read`, `autocad.write`.
2. Chỉ sau đó mới tạo hoặc kết nối lại app trong ChatGPT. DCR sẽ tạo một Auth0
   application mới có client ID bắt đầu bằng `tpc_`.
3. Nếu API bật RBAC, user đăng nhập cũng phải được gán hai permission trên qua
   role hoặc trực tiếp. Có thể bật **Add Permissions in the Access Token** để
   Auth0 thêm claim `permissions`; server chấp nhận cả claim `scope` và
   `permissions`.
4. Sau bất kỳ thay đổi nào về permission, ngắt liên kết OAuth cũ và đăng nhập
   lại. Access token đã phát hành không tự nhận thêm scope.

Nếu app DCR được tạo trước khi có default permissions, cách kiểm tra sạch nhất
là xóa app thử nghiệm đó ở cả ChatGPT và Auth0, rồi tạo lại sau khi đã lưu các
permission mặc định.

Không đặt client secret, access token hoặc private key vào repo. Resource server
chỉ cần issuer và audience; client credentials thuộc về ChatGPT/provider.

## Chạy server

Dùng hostname HTTPS ổn định đã trỏ vào máy này bằng named tunnel hoặc reverse
proxy. `cloudflared` Quick Tunnel của Phase 3 chỉ dùng demo vì hostname thay đổi
mỗi lần chạy.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-phase4-oauth.ps1 `
  -PublicBaseUrl "https://cad.example.com" `
  -OAuthIssuer "https://issuer.example" `
  -OAuthAudience "https://cad.example.com"
```

Luồng thường dùng trên máy Windows: `start_mcp_chatgpt.bat` (OAuth MCP) +
`start_cloudflare_tunnel.bat` (named tunnel). Wrapper hiện bật `-AllowExecuteLisp`
để ChatGPT có thể gọi freeform AutoLISP khi đã OAuth + File IPC.

Để tắt lại freeform LISP, bỏ `-AllowExecuteLisp` trong `start_mcp_chatgpt.bat`
hoặc gọi script không có switch đó.

Server sẽ phục vụ:

```text
https://cad.example.com/mcp
https://cad.example.com/.well-known/oauth-protected-resource
```

Khi app ChatGPT gửi request chưa có token, server trả `401` và
`WWW-Authenticate` trỏ tới protected-resource metadata. Khi token sai issuer,
audience, chữ ký, hạn hoặc JWKS thì server cũng trả `401`. Token thiếu scope
đọc trả `403 insufficient_scope` ngay ở lớp HTTP auth để client có thể yêu cầu
cấp quyền lại; token chỉ có `autocad.read` gọi operation ghi sẽ trả lỗi policy
scope.

## Biến môi trường chính

| Biến | Ý nghĩa |
|---|---|
| `AUTOCAD_MCP_REMOTE_PROFILE` | Phải là `production` |
| `AUTOCAD_MCP_AUTH_MODE` | Phải là `oauth` |
| `AUTOCAD_MCP_PUBLIC_BASE_URL` | Resource URL HTTPS, không có `/mcp` |
| `AUTOCAD_MCP_ALLOWED_HOSTS` | Hostname của resource URL |
| `AUTOCAD_MCP_OAUTH_ISSUER` | Issuer OIDC chính xác |
| `AUTOCAD_MCP_OAUTH_AUDIENCE` | Giá trị `aud` của access token |
| `AUTOCAD_MCP_OAUTH_SCOPES` | Mặc định `autocad.read autocad.write` |
| `AUTOCAD_MCP_ALLOW_EXECUTE_LISP` | `0` mặc định; `1` cho phép remote `execute_lisp` (rủi ro cao) |

## Checklist nghiệm thu

1. GET protected-resource metadata không cần token và trả đúng issuer/scopes.
2. MCP không token bị `401`.
3. Token hợp lệ + `autocad.read` gọi được operation đọc.
4. Token chỉ đọc không gọi được operation ghi.
5. Token hợp lệ + cả hai scope gọi được operation ghi.
6. Token sai issuer, audience, chữ ký, hết hạn và thiếu scope đều bị từ chối.
7. Refresh/revoke/đăng nhập được kiểm tra ở provider thật; repo này không giả lập authorization server.
