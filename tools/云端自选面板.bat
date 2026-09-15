@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PY=%~dp0..\..\..\..\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python

rem GitHub 令牌：优先读环境变量；没有则从 Temp 暂存文件读（本机约定）
if "%GH_PAT%"=="" (
  set /p GH_PAT=<"%TEMP%\astock_gh_token.txt"
)

echo.
echo   正在启动「云端自选管理」面板...
echo   浏览器会自动打开 http://127.0.0.1:8771/
echo   在这里增删的股票会同步到云端，下个交易时点生效。
echo   关闭本窗口即停止服务。
echo.
if "%GH_PAT%"=="" (
  echo   [提示] 未检测到 GH_PAT，增删只在本机生效、不会同步云端。
  echo.
)

"%PY%" -X utf8 -m pipeline.cloud_watch --port 8771 --open
pause
