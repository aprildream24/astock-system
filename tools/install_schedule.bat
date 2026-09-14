@echo off
rem ============================================================
rem  AStock 本地离线调度安装（2026-09-14 任务栏一键安装版）
rem  优先普通权限（当前用户级任务）；若被拒请右键管理员运行
rem  重复运行安全：/F 覆盖同名任务，不产生重复
rem ============================================================
chcp 65001 >nul
set ROOT=%~dp0..
set PY=C:\Users\Basshunter-j\.workbuddy\binaries\python\versions\3.13.12\python.exe

echo [1/6] 08:55 盘前计划推送（轻量抓取+build pre）
schtasks /Create /F /SC DAILY /ST 08:55 /TN "AStocker-Pre" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.fetch_daily --days 20 && \"%PY%\" -X utf8 -m pipeline.build --task pre" >nul 2>&1 && echo     [OK] || echo     [FAIL] 右键管理员重试
echo [2/6] 09:27 竞价裁决（build auction）
schtasks /Create /F /SC DAILY /ST 09:27 /TN "AStocker-Auction" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.build --task auction" >nul 2>&1 && echo     [OK] || echo     [FAIL] 右键管理员重试
echo [3/6] 15:40 收盘全链（全量抓取+close 推送+站点）
schtasks /Create /F /SC DAILY /ST 15:40 /TN "AStocker-Close" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.fetch_daily && \"%PY%\" -X utf8 -m pipeline.build --task close && \"%PY%\" -X utf8 -m pipeline.build --task site" >nul 2>&1 && echo     [OK] || echo     [FAIL] 右键管理员重试
echo [4/6] 20:10 收盘复盘（AI 叙事）
schtasks /Create /F /SC DAILY /ST 20:10 /TN "AStocker-Review" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 -m pipeline.build --task review" >nul 2>&1 && echo     [OK] || echo     [FAIL] 右键管理员重试
echo [5/6] 16:00 调度守护
schtasks /Create /F /SC DAILY /ST 16:00 /TN "AStocker-Watchdog1" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 tools\watchdog.py" >nul 2>&1 && echo     [OK] || echo     [FAIL] 右键管理员重试
echo [6/6] 21:00 调度守护
schtasks /Create /F /SC DAILY /ST 21:00 /TN "AStocker-Watchdog2" /TR "cmd /c cd /d %ROOT% && \"%PY%\" -X utf8 tools\watchdog.py" >nul 2>&1 && echo     [OK] || echo     [FAIL] 右键管理员重试

echo.
echo ---- 验证 ----
schtasks /Query /TN "AStocker-Pre" >nul 2>&1 && echo   08:55 Pre       OK
schtasks /Query /TN "AStocker-Auction" >nul 2>&1 && echo   09:27 Auction   OK
schtasks /Query /TN "AStocker-Close" >nul 2>&1 && echo   15:40 Close     OK
schtasks /Query /TN "AStocker-Review" >nul 2>&1 && echo   20:10 Review    OK
schtasks /Query /TN "AStocker-Watchdog1" >nul 2>&1 && echo   16:00 Watchdog  OK
schtasks /Query /TN "AStocker-Watchdog2" >nul 2>&1 && echo   21:00 Watchdog  OK
echo.
echo 全离线运行：抓取/构建/推送全在本地（PushPlus 发送需网络，与 GitHub 无关）
echo 任务栏可见：任务计划程序 ^> AStocker-*；卸载运行 uninstall_schedule.bat
pause
