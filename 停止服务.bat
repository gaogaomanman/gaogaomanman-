@echo off
chcp 65001 >nul
title AI工具合集 - 停止服务
echo ============================================
echo   AI工具合集 - 停止服务（统一工程）
echo ============================================
echo.

REM 统一工程只有一个服务（:8080）。顺带释放历史遗留端口，便于彻底收尾。
for %%P in (8080 4000 3001 3002 3003 3005 3007) do (
    powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %%P -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force; Write-Host ('  已停止 :%%P') } catch {} }" 2>nul
)

echo.
echo 服务已停止。
echo 提示：若某些端口未被占用，说明对应服务本来就没启动。
pause
