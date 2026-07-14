# VIDEODROP launcher — starts the local server and opens the browser.
# Usage:  right-click > Run with PowerShell, or:  .\run.ps1
# Optional: set a different browser for cookies (default firefox):
#   $env:VIDEODROP_BROWSER = "chrome"; .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
Write-Host "Starting VIDEODROP on http://127.0.0.1:7654 ..." -ForegroundColor Cyan
python app.py
