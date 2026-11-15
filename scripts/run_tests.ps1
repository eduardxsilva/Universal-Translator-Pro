# Scripts de Execução de Testes e Empacotamento Windows (PowerShell)
$ErrorActionPreference = "Stop"

Write-Host "=== Universal Translator Pro - Suite de Testes Automatizados ===" -ForegroundColor Cyan
$env:PYTHONPATH = "src;."
python -m unittest discover -s tests -p "test_*.py" -v

if (Get-Command pytest -ErrorAction SilentlyContinue) {
    Write-Host "`nExecutando pytest..." -ForegroundColor Cyan
    pytest -v
}
