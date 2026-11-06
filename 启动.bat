@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PORT=8765
set URL=http://127.0.0.1:%PORT%

REM ===== 已在运行则直接打开页面 =====
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing '%URL%/api/health' -TimeoutSec 2 | Out-Null; exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 goto open_running

REM ===== 检查虚拟环境 =====
if exist ".venv\Scripts\python.exe" goto startsvc
echo [job_finder] 未找到虚拟环境,正在初始化,仅首次需要,请耐心等待...
python -m venv --copies .venv
.venv\Scripts\python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

:startsvc
echo [job_finder] 正在启动服务,日志输出在弹出的 job_finder-server 控制台窗口...
start "job_finder-server" cmd /c ".venv\Scripts\python.exe -m app.main & pause"

set tries=0
:waitloop
ping -n 2 127.0.0.1 >nul
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing '%URL%/api/health' -TimeoutSec 2 | Out-Null; exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 goto ready
set /a tries=%tries%+1
if %tries% geq 30 goto timeout
goto waitloop

:timeout
echo [job_finder] 服务启动超时,请查看 job_finder-server 控制台窗口中的报错日志。
pause
exit /b 1

:ready
echo [job_finder] 服务就绪: %URL%(运行日志见 job_finder-server 控制台窗口)
start "" %URL%
ping -n 3 127.0.0.1 >nul
exit /b 0

:open_running
echo [job_finder] 服务已在运行,直接打开页面...
start "" %URL%
ping -n 2 127.0.0.1 >nul
exit /b 0
