@echo off
chcp 65001 >nul
title AI工具合集 - 停止开发调试
echo ============================================
echo   停止 开发调试 实例（只关 8090 / 5173）
echo   线上 8080 不受影响
echo ============================================
echo.
for %%P in (8090 5173) do (
    powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %%P -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force; Write-Host ('  已停止 :%%P') } catch {} }" 2>nul
)
echo.
echo 完成。
timeout /t 2 >nul
