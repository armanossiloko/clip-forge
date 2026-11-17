# Starts ClipForge in Docker. Run:  powershell -ExecutionPolicy Bypass -File .\docker-start.ps1
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Fail($msg) { Write-Host "`n$msg`n" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

# 1. Docker running?
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed. Install Docker Desktop:  winget install -e --id Docker.DockerDesktop  (then restart your PC)"
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting Docker Desktop..." -ForegroundColor Yellow
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) { Start-Process $dd }
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop didn't start. Open it from the Start menu, wait until it says 'running', then try again." }
}

# 2. Claude login token
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$envText = Get-Content .env -Raw
if ($envText -notmatch "(?m)^CLAUDE_CODE_OAUTH_TOKEN=\S+" -and $envText -notmatch "(?m)^ANTHROPIC_API_KEY=\S+") {
    Write-Host "`nClipForge needs a Claude login token for the container." -ForegroundColor Yellow
    Write-Host "In ANOTHER PowerShell window run:   claude setup-token"
    Write-Host "Log in in the browser, then copy the token it prints (starts with sk-ant-oat01-).`n"
    $secure = Read-Host "Paste the token here (it stays hidden)" -AsSecureString
    $token = [System.Net.NetworkCredential]::new("", $secure).Password.Trim()
    if (-not $token.StartsWith("sk-ant-")) { Fail "That doesn't look like a Claude token. Run the script again." }
    $envText = $envText -replace "(?m)^CLAUDE_CODE_OAUTH_TOKEN=.*$", "CLAUDE_CODE_OAUTH_TOKEN=$token"
    Set-Content .env $envText -NoNewline
    Write-Host "Saved to .env" -ForegroundColor Green
}

# 3. Build + start
New-Item -ItemType Directory -Force data, models, videos | Out-Null
Write-Host "`nBuilding and starting ClipForge (the first build takes a few minutes)..." -ForegroundColor Yellow
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed. Scroll up for the error." }

# 4. Wait until it answers, then open the browser
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest -UseBasicParsing http://localhost:8765/api/health -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 2 }
}
Start-Process "http://localhost:8765"
Write-Host "`nClipForge is running at http://localhost:8765" -ForegroundColor Green
Write-Host "Local video files go in the 'videos' folder. Finished clips are under data\jobs\...\clips."
Write-Host "Stop it with:  docker compose down`n"
