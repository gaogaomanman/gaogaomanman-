@echo off
chcp 65001 >nul
title AI工具合集 - 一键启动
setlocal enabledelayedexpansion

echo ============================================
echo   AI工具合集 - 一键启动（统一工程）
echo   单一 FastAPI 服务 :8080，同时托管前端页面
echo   服务以后台方式运行，不弹出黑框
echo ============================================
echo.

REM 回到 AI工具合集 所在目录
cd /d "%~dp0"

REM 日志目录
if not exist "%~dp0_logs" mkdir "%~dp0_logs"

REM 释放端口残留（避免重复启动/端口占用冲突）
REM   2026-09-23：这一步从「构建之后」提到「构建之前」——构建前需要先停掉占用
REM   dist 的服务进程，否则 dist 清不干净（见下面构建分支的说明）。
echo [清理] 释放 8080 端口残留进程...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }" 2>nul
ping -n 2 127.0.0.1 >nul

REM Python 解释器（统一工程使用 aitools 环境）
set "PY=%USERPROFILE%\miniconda3\envs\aitools\python.exe"
if not exist "%PY%" (
    echo !! 未找到 Python 环境：%PY%
    echo    请先创建 aitools 环境并安装 backend\requirements.txt 依赖
    pause
    exit /b 1
)

REM 前端构建产物检查（未构建则先构建）
REM   构建前先清空 dist：vite 自带的「清空 outDir」在本机被安全删除机制拦住（不报错也不清），
REM   旧 chunk 会一轮轮堆在 dist\assets 里（实测曾从 22 个涨到 223 个），
REM   干扰「产物里到底有没有某次改动」的排查。
REM   `npm run build` 另有 prebuild 钩子（scripts/clean-dist.mjs）兜底，手动构建同样生效；
REM   这里显式再清一次，是为了让单独跑本 .bat 也能得到干净产物。
if not exist "%~dp0frontend\dist\index.html" (
    echo [清理] 删除旧的前端产物 dist...
    powershell -NoProfile -Command "Remove-Item '%~dp0frontend\dist' -Recurse -Force -ErrorAction SilentlyContinue"
    echo [构建] 未发现前端产物 dist，正在构建...
    pushd "%~dp0frontend"
    call npm run build
    popd
)

REM 启动统一后端（同时提供 /api 与前端页面）
echo [1/1] 启动 统一服务 (:8080)...
powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8080' -WorkingDirectory '%~dp0backend' -WindowStyle Hidden -RedirectStandardOutput '%~dp0_logs\8080.out.log' -RedirectStandardError '%~dp0_logs\8080.log'"

echo.
echo [等待] 探测服务就绪（最多约 40 秒）...
call :wait_port 8080 40 "统一服务"

echo.
echo ============================================
echo   启动完成！正在打开 导航首页...
echo     导航首页   http://localhost:8080/
echo     达梦查询   http://localhost:8080/dm
echo     旧LIMS查询 http://localhost:8080/sqlserver
echo     数据看板   http://localhost:8080/board
echo     人员能力表 http://localhost:8080/personnel
echo     一单一库   http://localhost:8080/cma
echo ============================================
start "" "http://localhost:8080/"
timeout /t 2 >nul
echo 服务已在后台运行，任务栏不会出现黑框。
echo 关闭本窗口不影响服务运行。若要停止服务，双击 停止服务.bat。
pause
exit /b

REM ============ 子程序：等待端口可连通（TCP 探测） ============
:wait_port
set "port=%~1"
set "max=%~2"
set "name=%~3"
set /a n=0
:wait_port_loop
set /a n+=1
if !n! gtr %max% goto wp_timeout
powershell -NoProfile -Command "$c=New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',%port%); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 goto wp_retry
echo     %name%已就绪 ^(端口 %port%^)
exit /b 0
:wp_retry
ping -n 2 127.0.0.1 >nul
goto wait_port_loop
:wp_timeout
echo     警告: %name% 未就绪，请查看 _logs\8080.log
exit /b 0
