[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$McpPath = "/mcp",
    [ValidateSet("auto", "file_ipc", "ezdxf")]
    [string]$Backend = "auto",
    [string]$CloudflaredPath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogRoot = Join-Path ([IO.Path]::GetTempPath()) "autocad-mcp-phase3-$PID"
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

$serverProcess = $null
$tunnelProcess = $null

function Resolve-Executable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string]$ExplicitPath = ""
    )

    if ($ExplicitPath) {
        $resolved = Resolve-Path -LiteralPath $ExplicitPath -ErrorAction Stop
        return $resolved.Path
    }

    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $winGetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path -LiteralPath $winGetPackages) {
        $candidate = Get-ChildItem `
            -LiteralPath $winGetPackages `
            -Filter "$Name.exe" `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }

    return $null
}

function Start-LoggedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment,
        [Parameter(Mandatory = $true)]
        [string]$StdoutPath,
        [Parameter(Mandatory = $true)]
        [string]$StderrPath
    )

    $originalValues = @{}
    foreach ($name in $Environment.Keys) {
        $originalValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable(
            $name,
            [string]$Environment[$name],
            "Process"
        )
    }

    try {
        return Start-Process `
            -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -WorkingDirectory $RepoRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutPath `
            -RedirectStandardError $StderrPath `
            -PassThru
    }
    finally {
        foreach ($name in $originalValues.Keys) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $originalValues[$name],
                "Process"
            )
        }
    }
}

function Stop-ChildProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Name
    )

    if ($null -eq $Process) {
        return
    }

    try {
        if (-not $Process.HasExited) {
            Write-Host "Dừng $Name..." -ForegroundColor Yellow
            $Process.Kill()
            $Process.WaitForExit(5000) | Out-Null
        }
    }
    catch {
        Write-Warning "Không thể dừng $Name tự động: $($_.Exception.Message)"
    }
}

function Get-CombinedLog {
    param([string[]]$Paths)

    $chunks = foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) {
            Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
        }
    }
    return ($chunks -join "`n")
}

function Wait-ForLocalPort {
    param(
        [int]$TargetPort,
        [System.Diagnostics.Process]$Process
    )

    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($Process.HasExited) {
            throw "MCP server đã dừng sớm. Xem log server tại $LogRoot."
        }

        $listening = Test-NetConnection `
            -ComputerName "127.0.0.1" `
            -Port $TargetPort `
            -InformationLevel Quiet `
            -WarningAction SilentlyContinue
        if ($listening) {
            return
        }
        Start-Sleep -Milliseconds 500
    }

    throw "Không thấy MCP server lắng nghe tại 127.0.0.1:$TargetPort trong 30 giây."
}

try {
    $uvPath = Resolve-Executable -Name "uv"
    if (-not $uvPath) {
        throw "Không tìm thấy uv trong PATH. Hãy cài uv hoặc mở lại PowerShell sau khi cài."
    }

    $pythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        Write-Host "Chưa có .venv, chạy uv sync --locked..." -ForegroundColor Cyan
        & $uvPath sync --locked
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync --locked thất bại với mã $LASTEXITCODE."
        }
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Không tìm thấy Python project tại $pythonPath sau khi sync."
    }

    $cloudflaredPathResolved = Resolve-Executable `
        -Name "cloudflared" `
        -ExplicitPath $CloudflaredPath
    if (-not $cloudflaredPathResolved) {
        throw @"
Không tìm thấy cloudflared trong PATH.
Cài cloudflared theo tài liệu Cloudflare rồi chạy lại script:
https://developers.cloudflare.com/tunnel/downloads/
"@
    }

    $existingListener = Get-NetTCPConnection `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue
    if ($existingListener) {
        throw "Port $Port đang được process khác sử dụng. Hãy dừng process đó rồi chạy lại."
    }

    $baseEnvironment = @{
        AUTOCAD_MCP_BACKEND = $Backend
        AUTOCAD_MCP_TRANSPORT = "streamable-http"
        AUTOCAD_MCP_HOST = "127.0.0.1"
        AUTOCAD_MCP_PORT = [string]$Port
        AUTOCAD_MCP_PATH = $McpPath
        AUTOCAD_MCP_STATELESS_HTTP = "0"
        AUTOCAD_MCP_REMOTE_PROFILE = "dev"
        AUTOCAD_MCP_AUTH_MODE = "none"
        AUTOCAD_MCP_ALLOW_NO_AUTH = "1"
        AUTOCAD_MCP_ALLOWED_DIRS = ""
        AUTOCAD_MCP_ALLOWED_HOSTS = ""
        AUTOCAD_MCP_PUBLIC_BASE_URL = ""
        AUTOCAD_MCP_MAX_IMAGE_BYTES = "5242880"
        AUTOCAD_MCP_ONLY_TEXT = "0"
    }

    $serverStdout = Join-Path $LogRoot "server-initial.stdout.log"
    $serverStderr = Join-Path $LogRoot "server-initial.stderr.log"
    Write-Host "Khởi động MCP server local tại http://127.0.0.1:$Port$McpPath..." -ForegroundColor Cyan
    $serverProcess = Start-LoggedProcess `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "autocad_mcp") `
        -Environment $baseEnvironment `
        -StdoutPath $serverStdout `
        -StderrPath $serverStderr
    Wait-ForLocalPort -TargetPort $Port -Process $serverProcess

    $tunnelStdout = Join-Path $LogRoot "cloudflared.stdout.log"
    $tunnelStderr = Join-Path $LogRoot "cloudflared.stderr.log"
    Write-Host "Khởi động Cloudflare Quick Tunnel..." -ForegroundColor Cyan
    $tunnelProcess = Start-LoggedProcess `
        -FilePath $cloudflaredPathResolved `
        -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port") `
        -Environment @{} `
        -StdoutPath $tunnelStdout `
        -StderrPath $tunnelStderr

    $publicUrl = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($tunnelProcess.HasExited) {
            $tunnelLog = Get-CombinedLog -Paths @($tunnelStdout, $tunnelStderr)
            throw "cloudflared đã dừng trước khi tạo URL. Log: $tunnelLog"
        }

        $tunnelLog = Get-CombinedLog -Paths @($tunnelStdout, $tunnelStderr)
        $match = [regex]::Match(
            $tunnelLog,
            "https://[A-Za-z0-9-]+\.trycloudflare\.com"
        )
        if ($match.Success) {
            $publicUrl = $match.Value.TrimEnd("/")
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $publicUrl) {
        throw "Không lấy được URL trycloudflare.com trong 30 giây. Xem log tại $LogRoot."
    }

    $publicHost = ([Uri]$publicUrl).Host
    Write-Host "Đã có tunnel: $publicUrl" -ForegroundColor Green

    Stop-ChildProcess -Process $serverProcess -Name "MCP server bản đầu"

    $securedEnvironment = @{}
    foreach ($name in $baseEnvironment.Keys) {
        $securedEnvironment[$name] = $baseEnvironment[$name]
    }
    $securedEnvironment.AUTOCAD_MCP_ALLOWED_HOSTS = $publicHost
    $securedEnvironment.AUTOCAD_MCP_PUBLIC_BASE_URL = $publicUrl

    $securedServerStdout = Join-Path $LogRoot "server.stdout.log"
    $securedServerStderr = Join-Path $LogRoot "server.stderr.log"
    Write-Host "Khởi động lại MCP với Host allowlist: $publicHost" -ForegroundColor Cyan
    $serverProcess = Start-LoggedProcess `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "autocad_mcp") `
        -Environment $securedEnvironment `
        -StdoutPath $securedServerStdout `
        -StderrPath $securedServerStderr
    Wait-ForLocalPort -TargetPort $Port -Process $serverProcess

    Write-Host "" 
    Write-Host "Phase 3 dev đã sẵn sàng." -ForegroundColor Green
    Write-Host "URL nhập vào ChatGPT: $publicUrl$McpPath" -ForegroundColor Green
    Write-Host "Authentication: No Authentication" -ForegroundColor Yellow
    Write-Host "Chỉ operation đọc được phép; write vẫn bị policy chặn." -ForegroundColor Yellow
    Write-Host "Log server: $securedServerStderr" -ForegroundColor DarkGray
    Write-Host "Log tunnel: $tunnelStderr" -ForegroundColor DarkGray
    Write-Host "Giữ cửa sổ này mở trong lúc làm bước 5 và 6. Nhấn Ctrl+C để dừng." -ForegroundColor Cyan

    while ($true) {
        if ($serverProcess.HasExited) {
            throw "MCP server đã dừng. Xem log: $securedServerStderr"
        }
        if ($tunnelProcess.HasExited) {
            throw "cloudflared đã dừng. Xem log: $tunnelStderr"
        }
        Start-Sleep -Seconds 1
    }
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Stop-ChildProcess -Process $serverProcess -Name "MCP server"
    Stop-ChildProcess -Process $tunnelProcess -Name "Cloudflare tunnel"
    Write-Host "Log Phase 3 nằm tại: $LogRoot" -ForegroundColor DarkGray
}
