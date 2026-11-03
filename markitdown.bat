@echo off
setlocal enabledelayedexpansion
title MarkItDown Studio Web UI
echo ===================================================================
echo               KHOI DONG MARKITDOWN STUDIO WEB UI
echo ===================================================================

cd /d "%~dp0"

set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=python"
    ) else (
        where py >nul 2>nul
        if !errorlevel! equ 0 (
            set "PYTHON_EXE=py -3"
        )
    )
)

if "!PYTHON_EXE!"=="" (
    echo [LOI] Khong tim thay Python tren he thong hoac thu muc .venv!
    echo Vui long cai dat Python 3.10+ hoac tao moi moi truong .venv
    pause
    exit /b 1
)

echo [*] Su dung Python: !PYTHON_EXE!
echo [*] Dang khoi chay MarkItDown Studio tai http://127.0.0.1:8000 ...
echo [*] Nhan Ctrl+C de dung server bat ky luc nao.
echo.

!PYTHON_EXE! app.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [LOI] Chuong trinh bi dung dot ngot voi ma loi %ERRORLEVEL%.
    pause
)
