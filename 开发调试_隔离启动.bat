@echo off
chcp 65001 >nul
title AI工具合集 - 开发调试（隔离实例，不影响线上 8080）
setlocal enabledelayedexpansion

echo ============================================
echo   开发调试 - 隔离启动
echo   线上服务  :8080 保持不动，其它工具照常用
echo   调试后端  :8090（禁定时任务 + 独立数据目录）
echo   调试前端  :5173（热更新，改完即见）
echo ============================================
echo.

cd /d "%~dp0"
if not exist "%~dp0_logs" mkdir "%~dp0_logs"

set "PY=%USERPROFILE%\miniconda3\envs\aitools\python.exe"
if not exist "%PY%" (
    echo !! 未找到 Python 环境：%PY%
    pause
    exit /b 1
)

REM ---- 清理调试端口残留（只动 8090/5173，绝不碰 8080）----
echo [清理] 释放调试端口 8090 / 5173 ...
for %%P in (8090 5173) do (
    powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %%P -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }" 2>nul
)
ping -n 2 127.0.0.1 >nul

REM ---- 独立数据目录：调试期的表结构改动只影响 progress_dev ----
set "PROGRESS_DIR=%~dp0backend\data\progress_dev"

echo [1/2] 启动 调试后端 (:8090) ...
powershell -NoProfile -Command "$env:SCHEDULER_ENABLED='false'; $env:PROGRESS_DIR='%~dp0backend\data\progress_dev'; Start-Process -FilePath '%PY%' -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8090','--reload','--reload-dir','app' -WorkingDirectory '%~dp0backend' -WindowStyle Hidden -RedirectStandardOutput '%~dp0_logs\8090.out.log' -RedirectStandardError '%~dp0_logs\8090.log'"

echo [等待] 探测 8090 就绪 ...
for /l %%i in (1,1,30) do (
    powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8090/api/health' -TimeoutSec 2).StatusCode } catch { exit 1 }" >nul 2>nul
    if !errorlevel! equ 0 goto backend_ready
    ping -n 2 127.0.0.1 >nul
)
echo     警告: 8090 未就绪，请查看 _logs\8090.log
goto start_front
:backend_ready
echo     调试后端就绪 http://127.0.0.1:8090

:start_front
echo.
echo [2/2] 启动 调试前端 (:5173) ... 本窗口保持不关；按 Ctrl+C 停止前端
echo.
echo   调试页面   http://localhost:5173/progress
echo   线上页面   http://localhost:8080/       （未受影响，可继续使用）
echo   停止调试   双击 开发调试_停止.bat
echo.
set "VITE_API_TARGET=http://localhost:8090"
cd /d "%~dp0frontend"
call npm run dev

echo.
echo 前端已退出。调试后端仍在后台，如需停止请运行 开发调试_停止.bat
pause
