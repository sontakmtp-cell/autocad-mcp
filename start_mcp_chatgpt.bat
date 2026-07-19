@echo off
setlocal

rem Quick-start wrapper for the Phase 4 OAuth production MCP server.

cd /d "%~dp0"

where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo Khong tim thay Windows PowerShell.
    pause
    exit /b 1
)

if not exist "%~dp0scripts\run-phase4-oauth.ps1" (
    echo Khong tim thay scripts\run-phase4-oauth.ps1.
    pause
    exit /b 1
)

echo Dang kiem tra MCP hien tai tren cong 8765...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command ^
    "$listeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue); $processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique); foreach ($processId in $processIds) { $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $processId) -ErrorAction Stop; if ([string]$process.CommandLine -notmatch '(?i)-m\s+autocad_mcp(?:\s|$)') { throw ('Cong 8765 dang do process khac su dung: PID ' + $processId + ' - ' + $process.Name) }; Write-Host ('Dang dong AutoCAD MCP cu, PID ' + $processId + '...') -ForegroundColor Yellow; Stop-Process -Id $processId -Force -ErrorAction Stop }; $deadline = (Get-Date).AddSeconds(10); do { $remaining = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue); if ($remaining.Count -eq 0) { break }; Start-Sleep -Milliseconds 200 } while ((Get-Date) -lt $deadline); if ($remaining.Count -gt 0) { throw 'MCP cu chua giai phong cong 8765 sau 10 giay.' }; if ($processIds.Count -eq 0) { Write-Host 'Khong co MCP cu dang chay.' -ForegroundColor DarkGray }"

if errorlevel 1 (
    echo.
    echo Khong the dong MCP cu mot cach an toan. Khong khoi dong instance moi.
    pause
    exit /b 1
)

echo Dang khoi dong AutoCAD MCP Phase 4 cho ChatGPT...
echo URL MCP: https://cad.kythuatvang.com/mcp
echo Giu cua so nay mo trong luc su dung. Nhan Ctrl+C de dung.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass ^
    -File "%~dp0scripts\run-phase4-oauth.ps1" ^
    -PublicBaseUrl "https://cad.kythuatvang.com" ^
    -OAuthIssuer "https://dev-fmth5j5hp2e5sk3s.us.auth0.com/" ^
    -OAuthAudience "https://cad.kythuatvang.com/" ^
    -Backend auto

if errorlevel 1 (
    echo.
    echo MCP dung voi loi. Kiem tra log loi o phia tren.
    exit /b 1
)

endlocal
