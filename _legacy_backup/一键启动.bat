@echo off
chcp 65001 >nul
title AI工具合集 - 一键启动
setlocal enabledelayedexpansion

echo ============================================
echo   AI工具合集 - 一键启动
echo   服务将以后台方式运行，不弹出黑框
echo ============================================
echo.

REM 回到 AI工具合集 所在目录
cd /d "%~dp0"
REM 工作区根目录（AI工具合集 的上级）
set "ROOT=%cd%\.."
set "VBS=%~dp0_launch.vbs"
REM 便携版自带 node.exe（在 ROOT 根目录）；若存在则用它（免装Node），否则用系统 node
if exist "%ROOT%\node.exe" ( set "NODE=%ROOT%\node.exe" ) else ( set "NODE=node" )

REM 先释放所有相关端口残留进程（避免重复启动/端口占用冲突）
echo [清理] 释放旧端口进程...
for %%P in (8080 4000 3001 3003 3005 3007 3002) do (
    powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %%P -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force } catch {} }" 2>nul
)
REM 兜底：等清理生效
ping -n 2 127.0.0.1 >nul

REM 中间池日志目录
if not exist "%ROOT%\AI工具合集\_logs" mkdir "%ROOT%\AI工具合集\_logs"

REM 启动导航首页（8080）
echo [1/7] 启动 AI工具合集导航首页 (:8080)...
wscript "%VBS%" "%ROOT%\AI工具合集" "%ROOT%\AI工具合集\server.js" "%ROOT%\AI工具合集\_logs\nav.log" "%NODE%"

REM 启动中间池（4000）
echo [2/7] 启动 数据库连接中间池 (:4000)...
if exist "%ROOT%\unified-pool\server.js" (
    wscript "%VBS%" "%ROOT%\unified-pool" "%ROOT%\unified-pool\server.js" "%ROOT%\AI工具合集\_logs\pool.log" "%NODE%"
) else (
    echo      !! 未找到 unified-pool\server.js，跳过
)

REM 启动 5 个工具
echo [3/7] 启动 达梦数据库查询 (:3001)...
wscript "%VBS%" "%ROOT%\20260605160640" "%ROOT%\20260605160640\server.js" "%ROOT%\AI工具合集\_logs\3001.log" "%NODE%"

echo [4/7] 启动 SQL Server 查询 (:3003)...
wscript "%VBS%" "%ROOT%\sqlserver-query-tool" "%ROOT%\sqlserver-query-tool\server.js" "%ROOT%\AI工具合集\_logs\3003.log" "%NODE%"

echo [5/7] 启动 数据看板 (:3005, FastAPI + Vue3)...
set "PY=%USERPROFILE%\miniconda3\envs\dashboard\python.exe"
if exist "%ROOT%\数据看板\backend\app\main.py" (
    if exist "%PY%" (
        powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','3005' -WorkingDirectory '%ROOT%\数据看板\backend' -WindowStyle Hidden -RedirectStandardOutput '%ROOT%\AI工具合集\_logs\3005.out.log' -RedirectStandardError '%ROOT%\AI工具合集\_logs\3005.log'"
    ) else (
        echo     !! 未找到 conda 环境 dashboard: %PY%
        echo        请先创建该环境并安装 backend\requirements.txt 依赖
    )
) else (
    echo     !! 未找到新版看板 %ROOT%\数据看板\backend\app\main.py，跳过
)

echo [6/7] 启动 人员能力表梳理 (:3007)...
wscript "%VBS%" "%ROOT%\人员能力表梳理" "%ROOT%\人员能力表梳理\server.js" "%ROOT%\AI工具合集\_logs\3007.log" "%NODE%"

echo [7/7] 启动 一单一库核对 (:3002)...
if exist "%ROOT%\cma-checker\server.js" (
    wscript "%VBS%" "%ROOT%\cma-checker" "%ROOT%\cma-checker\server.js" "%ROOT%\AI工具合集\_logs\3002.log" "%NODE%"
) else (
    echo      !! 未找到 cma-checker\server.js，跳过
)

echo.
echo [等待] 探测各服务就绪（最多约30秒）...
REM 探测方式：TCP 端口连通性检测（不依赖 HTTP 状态码，避免 404 误判）
REM 注意：本段刻意不用括号块，避免批处理崩溃

REM 等待中间池就绪（最多 30 秒）
call :wait_port 4000 30 "中间池"

REM 等待 5 个工具就绪（逐个，最多 15 秒）
call :wait_port 3001 15 "达梦查询"
call :wait_port 3003 15 "SQL查询"
call :wait_port 3005 15 "数据看板"
call :wait_port 3007 15 "人员能力"
call :wait_port 3002 15 "一单一库核对"

echo.
echo ============================================
echo   启动完成！正在打开 导航首页...
echo ============================================
start "" "http://localhost:8080/"
timeout /t 2 >nul
echo 所有服务已在后台运行，任务栏不会出现黑框。
echo 关闭本窗口不影响服务运行。若要停止服务，双击 停止服务.bat。
pause
exit /b

REM ============ 子程序：等待端口可连通（TCP 探测） ============
REM 用法：call :wait_port 端口 最大秒数 名称
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
echo     警告: %name% 未就绪，继续...
exit /b 0
