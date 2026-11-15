@echo off
chcp 65001 >nul
title Universal Translator Pro — Gerador de Executavel (.EXE)
cd /d "%~dp0"

echo ============================================================================
echo   UNIVERSAL TRANSLATOR PRO - COMPILADOR AUTOMATICO DE EXECUTAVEL (.EXE)
echo ============================================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Criando ambiente virtual Python...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
)

echo [2/4] Instalando dependencias e PyInstaller...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller

echo [3/4] Compilando UniversalTranslatorPro.exe (Arquivo Unico Standalone)...
".venv\Scripts\pyinstaller.exe" --noconfirm --clean UniversalTranslatorPro.spec

if exist "dist\UniversalTranslatorPro.exe" (
    echo [4/4] Copiando UniversalTranslatorPro.exe para a pasta principal...
    copy /Y "dist\UniversalTranslatorPro.exe" "UniversalTranslatorPro.exe" >nul
    echo.
    echo ============================================================================
    echo   SUCESSO! O executavel foi gerado em:
    echo   - %~dp0UniversalTranslatorPro.exe
    echo   - %~dp0dist\UniversalTranslatorPro.exe
    echo ============================================================================
    echo.
    echo Iniciando UniversalTranslatorPro.exe...
    start "" "%~dp0UniversalTranslatorPro.exe"
) else (
    echo.
    echo [ERRO] A compilacao falhou. Verifique as mensagens acima.
)

pause
