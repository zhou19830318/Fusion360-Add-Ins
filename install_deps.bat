@echo off
setlocal enabledelayedexpansion
title AI Fusion — Dependency Installer

REM ===========================================================================
REM  AI Fusion — Windows Dependency Installer
REM
REM  Finds Fusion 360's bundled Python, bootstraps pip if missing, and
REM  installs flask + requests. Run once before first use, or whenever
REM  Fusion updates to a new version that ships a fresh Python environment.
REM
REM  Usage:
REM      install_deps.bat              (normal)
REM      install_deps.bat --admin      (force admin-recommended mode)
REM ===========================================================================

echo.
echo  ================================================================
echo   AI Fusion — Dependency Installer
echo  ================================================================
echo.

REM ── Step 0: Detect admin privileges ──────────────────────────────────
net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
    if /i "%~1"=="--admin" (
        echo [WARN] Not running as Administrator. Permission errors may occur.
        echo        Right-click this file ^> "Run as Administrator" if install fails.
        echo.
    ) else (
        echo [INFO] Not running as Administrator. If this fails, re-run as Admin.
        echo.
    )
)

REM ── Step 1: Find Python ──────────────────────────────────────────────
set "PYTHON_EXE="

REM Priority 1: Fusion 360 bundled Python (most reliable)
if exist "%LOCALAPPDATA%\Autodesk\webdeploy\production" (
    echo [1/4] Searching for Fusion 360 Python...
    for /d %%d in ("%LOCALAPPDATA%\Autodesk\webdeploy\production\*") do (
        if exist "%%d\Python\python.exe" (
            set "PYTHON_EXE=%%d\Python\python.exe"
            echo        Found: !PYTHON_EXE!
        )
    )
)

REM Priority 2: System Python (fallback — may install to wrong site-packages)
if not defined PYTHON_EXE (
    echo [1/4] Fusion Python not found. Searching system PATH...
    where python >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        for /f "delims=" %%p in ('where python') do (
            set "PYTHON_EXE=%%p"
            echo        Found: !PYTHON_EXE!
            goto :found_python
        )
    )
    where python3 >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        for /f "delims=" %%p in ('where python3') do (
            set "PYTHON_EXE=%%p"
            echo        Found: !PYTHON_EXE!
            goto :found_python
        )
    )
)

:found_python
if not defined PYTHON_EXE (
    echo.
    echo [ERROR] No Python executable found.
    echo.
    echo   Fusion 360 bundles its own Python at:
    echo     %%LOCALAPPDATA%%\Autodesk\webdeploy\production\*\Python\python.exe
    echo.
    echo   If Fusion is installed but the path is missing, fully quit and
    echo   restart Fusion, then run this script again.
    echo.
    echo   Alternatively, install Python 3.8+ from https://python.org,
    echo   then run: pip install flask requests
    echo.
    pause
    exit /b 1
)

REM Verify the Python works
echo.
echo [2/4] Verifying Python...
"%PYTHON_EXE%" --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo        [WARN] Python at "%PYTHON_EXE%" does not respond.
    echo        Trying fallback paths...
    set "PYTHON_EXE="
    goto :find_python_fallback
)
echo        OK: "%PYTHON_EXE%"
"%PYTHON_EXE%" --version 2>&1 | findstr /c:"Python" >nul
if %ERRORLEVEL% neq 0 (
    echo        [WARN] Could not determine Python version.
)

goto :check_pip_available

:find_python_fallback
REM Last resort: scan webdeploy again more aggressively
if exist "%LOCALAPPDATA%\Autodesk\webdeploy\production" (
    for /f "delims=" %%d in ('dir /b /s "%LOCALAPPDATA%\Autodesk\webdeploy\production\python.exe" 2^>nul') do (
        set "PYTHON_EXE=%%d"
        echo        Found: !PYTHON_EXE!
        goto :check_pip_available
    )
)
echo        [ERROR] Cannot find any usable Python.
pause
exit /b 1


REM ── Step 2: Check / bootstrap pip ───────────────────────────────────
:check_pip_available
echo.
echo [3/4] Checking pip availability...
"%PYTHON_EXE%" -m pip --version >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo        pip is available.
    goto :install_packages
)

echo        pip not found. Bootstrapping via ensurepip...
"%PYTHON_EXE%" -c "import ensurepip; ensurepip.bootstrap()" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo        pip bootstrap successful.
    goto :install_packages
)

REM ensurepip also failed — last attempt with manual pip install
echo        ensurepip failed. Trying manual pip bootstrap...
"%PYTHON_EXE%" -c "import urllib.request, os, sys; url='https://bootstrap.pypa.io/get-pip.py'; f=os.path.join(os.environ.get('TEMP','.'),'get-pip.py'); urllib.request.urlretrieve(url,f); exec(open(f).read())" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo        Manual pip bootstrap successful.
    goto :install_packages
)

echo.
echo [ERROR] Cannot install pip. All methods failed.
echo.
echo   Try running this script AS ADMINISTRATOR.
echo   Or install Python manually from https://python.org
echo.
pause
exit /b 1


REM ── Step 3: Install packages ────────────────────────────────────────
:install_packages
echo.
echo [4/4] Installing flask and requests...
"%PYTHON_EXE%" -m pip install --quiet flask requests 2>&1
set "PIP_EXIT=%ERRORLEVEL%"

if %PIP_EXIT% equ 0 (
    echo.
    echo  ================================================================
    echo   SUCCESS! Dependencies installed.
    echo  ================================================================
    echo.
    echo   Verified packages:
    "%PYTHON_EXE%" -c "import flask; print('    flask', flask.__version__)" 2>nul
    "%PYTHON_EXE%" -c "import requests; print('    requests', requests.__version__)" 2>nul
    echo.
    echo   Restart Fusion 360, then Shift+S ^> Add-Ins ^> AIFusion ^> Run.
    echo.
    goto :done
)

REM pip failed — show diagnostics
echo.
echo  ================================================================
echo   INSTALL FAILED (exit code %PIP_EXIT%)
echo  ================================================================
echo.
echo   Possible causes and fixes:
echo.
echo   1. Permission denied
echo      ^> Right-click this .bat file ^> "Run as Administrator"
echo.
echo   2. SSL / certificate error (corporate proxy or firewall)
echo      ^> Run: "%PYTHON_EXE%" -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org flask requests
echo.
echo   3. Offline or no internet
echo      ^> Connect to the internet and re-run this script.
echo.
echo   4. Fusion Python version is too old
echo      ^> Update Fusion 360 to the latest version.
echo.
pause


:done
endlocal
exit /b %PIP_EXIT%
