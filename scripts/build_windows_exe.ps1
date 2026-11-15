# Script de Empacotamento Executável Windows (.exe Standalone Único) via PyInstaller
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot\..

Write-Host "=== Compilando UniversalTranslatorPro.exe (Arquivo Único Standalone) ===" -ForegroundColor Cyan

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
& ".\.venv\Scripts\pyinstaller.exe" --noconfirm --clean UniversalTranslatorPro.spec

if (Test-Path "dist\UniversalTranslatorPro.exe") {
    Copy-Item "dist\UniversalTranslatorPro.exe" -Destination ".\UniversalTranslatorPro.exe" -Force
    Write-Host "`nExecutável gerado com sucesso em:" -ForegroundColor Green
    Write-Host " -> $(Resolve-Path .\UniversalTranslatorPro.exe)" -ForegroundColor Yellow
}
