# Windows development setup. Run from the repo root in PowerShell:  .\scripts\setup_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "== python venv (3.12 preferred, matches Ubuntu 24.04 on the board)"
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -e ".[piper,sapi,dev]"

Write-Host "== model server"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Host "Ollama found - pulling gemma4:e4b (same model family as on the IQ-9075)"
    ollama pull gemma4:e4b
} else {
    Write-Host "Ollama not found. Install from https://ollama.com or run llama-server with a Gemma 4 GGUF + mmproj on port 11434."
}

Write-Host "== config"
if (-not (Test-Path config.yaml)) { Copy-Item config.example.yaml config.yaml }
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host ">> edit .env for the Eufy bridge, then: docker compose up -d" }

Write-Host ""
Write-Host "Run:   .\.venv\Scripts\python -m screaming_camera"
Write-Host "Panel: http://localhost:8080"
