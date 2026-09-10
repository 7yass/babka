# Shares the panel on a temporary public https URL via Cloudflare quick tunnel.
# 1. Start the panel first:  python webpanel/app.py
# 2. Run this script:        .\share-panel.ps1 [-Port 8080]
#    (downloads cloudflared.exe on first run if missing)
# 3. Send the https://....trycloudflare.com URL + panel password. Stop sharing: Ctrl+C.
param([int]$Port = 8080)

$ErrorActionPreference = 'Stop'
$binDir = Join-Path $PSScriptRoot '.bin'
$cf = Join-Path $binDir 'cloudflared.exe'
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path -LiteralPath $cf)) {
        Write-Output 'Downloading cloudflared (first run only)...'
        New-Item -ItemType Directory -Path $binDir -Force | Out-Null
        Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cf
    }
    $cfCmd = $cf
} else {
    $cfCmd = 'cloudflared'
}
# Panel listens on localhost by default; tunnel only needs local access.
Write-Output "Opening temporary public URL for http://127.0.0.1:$Port ..."
& $cfCmd tunnel --url "http://127.0.0.1:$Port"
