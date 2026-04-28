$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Test-Path (Join-Path $ProjectRoot "server.py"))) {
    Write-Host "server.py was not found beside this script." -ForegroundColor Red
    Write-Host "Please run the launcher from E:\project\cut, or create a shortcut to E:\project\cut\start.bat."
    exit 1
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found. Please install Python 3.9+ and make sure python is in PATH." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "outputs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "cache\\transcripts") | Out-Null

Write-Host ""
Write-Host "Live clip system is starting..." -ForegroundColor Green
Write-Host "Project root: $ProjectRoot"
Write-Host "Logs: $(Join-Path $ProjectRoot 'logs')"
Write-Host "The browser will open automatically. If port 8787 is busy, watch this window for the actual URL."
Write-Host "Press Ctrl+C to stop"
Write-Host ""

python server.py
