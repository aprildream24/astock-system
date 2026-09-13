@echo off
rem 本地计划任务一键注册（吸收自原项目 install_schedule.bat）
rem 需要管理员权限运行。任务：
rem   15:40 收盘抓取+构建+推送   20:10 复盘叙事   16:00/21:00 调度守护
chcp 65001 >nul
set ROOT=%~dp0..
schtasks /Create /F /SC DAILY /ST 15:40 /TN "AStocker-Close" /TR "cmd /c cd /d %ROOT% && py -X utf8 -m pipeline.fetch_daily && py -X utf8 -m pipeline.build --task close && py -X utf8 -m pipeline.build --task site" >nul && echo [OK] AStocker-Close 15:40
schtasks /Create /F /SC DAILY /ST 20:10 /TN "AStocker-Review" /TR "cmd /c cd /d %ROOT% && py -X utf8 -m pipeline.build --task review" >nul && echo [OK] AStocker-Review 20:10
schtasks /Create /F /SC DAILY /ST 16:00 /TN "AStocker-Watchdog1" /TR "cmd /c cd /d %ROOT% && py -X utf8 tools\watchdog.py" >nul && echo [OK] AStocker-Watchdog1 16:00
schtasks /Create /F /SC DAILY /ST 21:00 /TN "AStocker-Watchdog2" /TR "cmd /c cd /d %ROOT% && py -X utf8 tools\watchdog.py" >nul && echo [OK] AStocker-Watchdog2 21:00
echo 完成。卸载：schtasks /Delete /TN AStocker-Close /F（其余同理）
