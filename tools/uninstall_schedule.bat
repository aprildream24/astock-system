@echo off
rem AStock 本地调度卸载（2026-09-14）
chcp 65001 >nul
schtasks /Delete /TN "AStocker-Pre" /F >nul 2>&1 && echo [OK] 删除 Pre
schtasks /Delete /TN "AStocker-Auction" /F >nul 2>&1 && echo [OK] 删除 Auction
schtasks /Delete /TN "AStocker-Close" /F >nul 2>&1 && echo [OK] 删除 Close
schtasks /Delete /TN "AStocker-Review" /F >nul 2>&1 && echo [OK] 删除 Review
schtasks /Delete /TN "AStocker-Watchdog1" /F >nul 2>&1 && echo [OK] 删除 Watchdog1
schtasks /Delete /TN "AStocker-Watchdog2" /F >nul 2>&1 && echo [OK] 删除 Watchdog2
echo 完成。
pause
