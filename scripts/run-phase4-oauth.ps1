[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,
    [Parameter(Mandatory = $true)]
    [string]$OAuthIssuer,
    [Parameter(Mandatory = $true)]
    [string]$OAuthAudience,
    [string]$OAuthScopes = "autocad.read autocad.write",
    [int]$Port = 8765,
    [string]$McpPath = "/mcp",
    [ValidateSet("auto", "file_ipc", "ezdxf")]
    [string]$Backend = "auto",
    # High risk: lets authenticated remote clients (e.g. ChatGPT) run arbitrary AutoLISP.
    # Requires File IPC + OAuth write scope. Off by default.
    [switch]$AllowExecuteLisp
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

$resourceUri = [Uri]$PublicBaseUrl
if ($resourceUri.Scheme -ne "https") {
    throw "PublicBaseUrl must use HTTPS."
}
if ($resourceUri.AbsolutePath -ne "/" -or $resourceUri.Query -or $resourceUri.Fragment) {
    throw "PublicBaseUrl must be the HTTPS origin only, for example https://cad.example.com."
}

$issuerUri = [Uri]$OAuthIssuer
if ($issuerUri.Scheme -ne "https") {
    throw "OAuthIssuer must use HTTPS."
}

$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv was not found. Install uv or run this script from the project environment."
    }
    & $uv.Source sync --locked
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python was not found at $pythonPath."
}

$environment = @{
    AUTOCAD_MCP_BACKEND = $Backend
    AUTOCAD_MCP_TRANSPORT = "streamable-http"
    AUTOCAD_MCP_HOST = "127.0.0.1"
    AUTOCAD_MCP_PORT = [string]$Port
    AUTOCAD_MCP_PATH = $McpPath
    AUTOCAD_MCP_STATEFUL_HTTP = "0"
    AUTOCAD_MCP_REMOTE_PROFILE = "production"
    AUTOCAD_MCP_AUTH_MODE = "oauth"
    AUTOCAD_MCP_ALLOW_NO_AUTH = "0"
    AUTOCAD_MCP_ALLOW_EXECUTE_LISP = $(if ($AllowExecuteLisp) { "1" } else { "0" })
    AUTOCAD_MCP_ALLOWED_HOSTS = $resourceUri.Host
    AUTOCAD_MCP_PUBLIC_BASE_URL = $resourceUri.GetLeftPart([UriPartial]::Authority)
    AUTOCAD_MCP_OAUTH_ISSUER = $OAuthIssuer.TrimEnd("/")
    AUTOCAD_MCP_OAUTH_AUDIENCE = $OAuthAudience
    AUTOCAD_MCP_OAUTH_SCOPES = $OAuthScopes
    AUTOCAD_MCP_ALLOWED_DIRS = ""
    AUTOCAD_MCP_MAX_IMAGE_BYTES = "5242880"
    AUTOCAD_MCP_ONLY_TEXT = "0"
}

$originalValues = @{}
foreach ($name in $environment.Keys) {
    $originalValues[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    [Environment]::SetEnvironmentVariable($name, [string]$environment[$name], "Process")
}

try {
    Write-Host "Starting AutoCAD MCP OAuth production profile..." -ForegroundColor Cyan
    Write-Host "Resource: $($environment.AUTOCAD_MCP_PUBLIC_BASE_URL)" -ForegroundColor Green
    Write-Host "MCP endpoint: $($environment.AUTOCAD_MCP_PUBLIC_BASE_URL)$McpPath" -ForegroundColor Green
    Write-Host "Issuer: $($environment.AUTOCAD_MCP_OAUTH_ISSUER)" -ForegroundColor DarkGray
    Write-Host "Scopes: $($environment.AUTOCAD_MCP_OAUTH_SCOPES)" -ForegroundColor DarkGray
    Write-Host "No Authentication is disabled. Press Ctrl+C to stop." -ForegroundColor Yellow
    if ($AllowExecuteLisp) {
        Write-Host "WARNING: execute_lisp ENABLED for remote OAuth clients (ALLOW_EXECUTE_LISP=1)." -ForegroundColor Yellow
        Write-Host "Needs File IPC (AutoCAD running) + OAuth scope autocad.write." -ForegroundColor Yellow
    }
    else {
        Write-Host "execute_lisp is disabled remotely (default). Pass -AllowExecuteLisp to enable." -ForegroundColor DarkGray
    }
    & $pythonPath -m autocad_mcp
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    foreach ($name in $originalValues.Keys) {
        [Environment]::SetEnvironmentVariable($name, $originalValues[$name], "Process")
    }
}
