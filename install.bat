@echo off
setlocal enabledelayedexpansion
title Fusion360-Add-Ins (AI Fusion) - One-click Installer

echo ============================================================
echo   Fusion360-Add-Ins 一键部署脚本
echo   (AI Fusion - Fusion 360 的 AI 副驾驶插件)
echo ============================================================
echo.

rem ---- [1/4] 定位 Fusion 的 AddIns 目录 ----
if defined APPDATA (
  set "ADDINS=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns"
) else (
  set "ADDINS=%USERPROFILE%\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns"
)
if not exist "%ADDINS%" mkdir "%ADDINS%"
set "TARGET=%ADDINS%\Fusion360-Add-Ins"

rem 脚本所在目录(去掉结尾反斜杠)
set "CURRENT=%~dp0"
if "%CURRENT:~-1%"=="\" set "CURRENT=%CURRENT:~0,-1%"

echo [1/4] AddIns 目录: %ADDINS%
echo.

rem ---- [2/4] 检查是否已安装 / 是否需要复制 ----
set "FOUND="
if exist "%TARGET%\AIFusion.manifest"      set "FOUND=%TARGET%"
if not defined FOUND if exist "%CURRENT%\AIFusion.manifest" set "FOUND=%CURRENT%"
if not defined FOUND (
  rem 扫描 AddIns 下已有副本,避免重复安装
  for /d %%D in ("%ADDINS%\*") do (
    if not defined FOUND if exist "%%~D\AIFusion.manifest" set "FOUND=%%~D"
  )
)

if defined FOUND (
  echo [2/4] 检测到插件已存在: %FOUND%
  echo        已跳过文件复制,直接进行检查
  set "DEPLOY_DIR=%FOUND%"
) else (
  echo [2/4] 正在把插件复制到: %TARGET%
  robocopy "%CURRENT%" "%TARGET%" /E /XD .git __pycache__ .inscode /XF aifusion_debug.log aifusion_log.txt log_tail.txt config.json *.tmp >nul
  if %errorlevel% GEQ 8 (
    echo        [!] 复制失败,可能是权限不足。请右键本脚本后选“以管理员身份运行”再试。
    echo.
    pause
    exit /b 1
  )
  set "DEPLOY_DIR=%TARGET%"
)
echo.

rem ---- [3/4] 安装 Python 依赖(flask / requests) ----
echo [3/4] 检查 Python 依赖 ...
where python >nul 2>nul
if %errorlevel%==0 (
  python -m pip install --quiet flask requests >nul 2>nul
  if %errorlevel%==0 (
    echo        OK: flask + requests 已就绪
  ) else (
    echo        [!] pip 安装未成功,插件首次启动时会自动安装
  )
) else (
  echo        [!] 未检测到系统 Python。
  echo            插件首次启动时会自动安装依赖
)
echo.

rem ---- [4/4] 配置 API Key(自动检测,交互式引导) ----
echo [4/4] 检查 API Key 配置 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CURRENT%\tools\setup_config.ps1" -DeployDir "%DEPLOY_DIR%"
echo.

rem ---- 完成 ----
echo ============================================================
echo   部署完成!
echo ============================================================
echo.
echo   接下来照做三步即可:
echo     1. 完全退出并重启 Fusion 360
echo     2. 按 Shift+S 打开 Add-Ins 窗口
echo        找到 "Fusion360-Add-Ins" -> 点 Run -> 勾选 Run on Startup
echo     3. 右侧出现聊天面板;点右上角齿轮(设置)可更换模型/填 Key
echo.
echo   插件安装位置: %DEPLOY_DIR%
echo.
pause
endlocal