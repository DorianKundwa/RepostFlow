@echo off
setlocal enabledelayedexpansion
title RepostFlow Production Server

:: ============================================================
::  RepostFlow - YouTube Multi-Channel Video Automation Platform
::  Tagline: Upload Once. Distribute Everywhere.
:: ============================================================

set PORT=5000
set HOST=127.0.0.1

echo.
echo ============================================================
echo   RepostFlow - Starting in Production Mode (Waitress WSGI)
echo ============================================================
echo.

:: ------------------------------------------------------------
:: 1. Check and Free Target Port if in use
:: ------------------------------------------------------------
echo [1/3] Checking if port %PORT% is available...

:: Method A: PowerShell Get-NetTCPConnection (fast and exact)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$conns = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; " ^
  "if ($conns) { " ^
  "    foreach ($c in $conns) { " ^
  "        $p = $c.OwningProcess; " ^
  "        Write-Host ('   [!] Port %PORT% is occupied by PID ' + $p + '. Terminating...'); " ^
  "        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue; " ^
  "    } " ^
  "    Start-Sleep -Seconds 1; " ^
  "}"

:: Method B: Fallback netstat check
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    if "%%p" neq "" (
        echo    [!] Freeing port %PORT% from PID %%p...
        taskkill /F /PID %%p >nul 2>&1
    )
)

echo [OK]  Port %PORT% is ready for RepostFlow.
echo.

:: ------------------------------------------------------------
:: 2. Verify Python Virtual Environment
:: ------------------------------------------------------------
echo [2/3] Checking virtual environment...
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found at 'venv\Scripts\python.exe'.
    echo Please create the environment first:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo [OK]  Virtual environment verified.
echo.

:: ------------------------------------------------------------
:: 3. Launch RepostFlow in Production Mode (Waitress WSGI)
:: ------------------------------------------------------------
echo [3/3] Launching production server...
echo.
echo ============================================================
echo   RepostFlow is live!
echo   Open your browser at: http://localhost:%PORT%/
echo.
echo   Press Ctrl+C to stop the server.
echo ============================================================
echo.

venv\Scripts\python.exe deploy\start_windows.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Server exited with status code %ERRORLEVEL%.
    pause
)
