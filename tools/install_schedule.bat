@echo off
rem 本地计划任务一键注册（GitHub cron 不可靠时的兜底通道）
rem 需要管理员权限运行。任务（对齐 GitHub stock.yml 的关键时点）：
rem   08:55 盘前计划推送   09:27 竞价裁决   15:40 收盘抓取+构建+推送
rem   20:10 复盘叙事      16:00/21:00 调度守护
rem 说明：
rem   - 交易日守门内建（非交易日自动空转，不会推假数据）
rem   - 推送去重内建（当日同任务已推过则跳过，与 CI 互为兜底不重复）
chcp 65001 >nul
set ROOT=%~dp0..
set PY=C:\Users\Basshunter-j\.workbuddy\binaries\python\versions\3.13.12\python.exe

schtasks /Create /F /SC DAILY /ST 08:55 /TN "AStocker-Pre" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.fetch_daily --days 20 && \"%PY%\" -X utf8 -m pipeline.build --task pre" >nul && echo [OK] AStocker-Pre 08:55
schtasks /Create /F /SC DAILY /ST 09:27 /TN "AStocker-Auction" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.build --task auction" >nul && echo [OK] AStocker-Auction 09:27
schtasks /Create /F /SC DAILY /ST 15:40 /TN "AStocker-Close" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.fetch_daily && \"%PY%\" -X utf8 -m pipeline.build --task close && \"%PY%\" -X utf8 -m pipeline.build --task site" >nul && echo [OK] AStocker-Close 15:40
schtasks /Create /F /SC DAILY /ST 20:10 /TN "AStocker-Review" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.build --task review" >nul && echo [OK] AStocker-Review 20:10
schtasks /Create /F /SC DAILY /ST 16:00 /TN "AStocker-Watchdog1" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 tools\watchdog.py" >nul && echo [OK] AStocker-Watchdog1 16:00
schtasks /Create /F /SC DAILY /ST 21:00 /TN "AStocker-Watchdog2" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 tools\watchdog.py" >nul && echo [OK] AStocker-Watchdog2 21:00
echo.
echo 完成。卸载：schtasks /Delete /TN AStocker-Pre /F（其余同理）
