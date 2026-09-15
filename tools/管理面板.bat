@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PY=%~dp0..\..\..\..\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo.
echo   正在启动「自选 / 持仓 / 用户权限」管理面板...
echo   浏览器会自动打开 http://127.0.0.1:8770/
echo   关闭本窗口即停止服务。
echo.
"%PY%" -X utf8 -m pipeline.admin --port 8770
pause
