@echo off
chcp 65001 >nul
title AI工具合集 - 停止服务
echo ============================================
echo   AI工具合集 - 停止所有服务
echo ============================================
echo.

REM 停止 导航首页 + 中间池 + 5 个工具
for %%P in (8080 4000 3001 3003 3005 3007 3002) do (
    powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %%P -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force; Write-Host ('  已停止 :%%P') } catch {} }" 2>nul
)

echo.
echo 所有服务已停止。
echo 提示：若某些端口未被占用，说明对应服务本来就没启动。
pause
