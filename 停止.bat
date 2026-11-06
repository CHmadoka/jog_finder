@echo off
chcp 65001 >nul
setlocal
set PORT=8765

REM ===== 结束占用服务端口的进程 =====
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('[job_finder] 停止进程 PID=' + $_); Stop-Process -Id $_ -Force }" 2>nul

ping -n 2 127.0.0.1 >nul
powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:%PORT%/api/health' -TimeoutSec 2 | Out-Null; exit 0}catch{exit 1}" >nul 2>&1
if %errorlevel%==0 (
    echo [job_finder] 停止失败,端口仍被占用,请手动检查任务管理器。
) else (
    echo [job_finder] 服务已停止。
)
ping -n 2 127.0.0.1 >nul
exit /b 0
