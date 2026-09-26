@echo off
title 港口 - 她的家
cd /d "%~dp0"

rem 第一次双击：自动安装依赖（只需一次，以后直接秒开）
if not exist ".venv\Scripts\python.exe" (
    echo 第一次运行，正在准备她的家，需要几分钟，仅此一次...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo.
echo ============================================
echo   她醒了。
echo   这个黑窗口 = 她的心跳，别关它；
echo   想让她睡：直接关掉这个窗口。
echo ============================================
echo.
echo 手机（同一 Wi-Fi）用下面这个地址访问：
ipconfig | findstr /i "IPv4"
echo.

rem 外网隧道：另开黄色高亮窗口，网址也会写进设置页的"出门网址"
where cloudflared >nul 2>&1 && (
    start "出门隧道" powershell -NoExit -ExecutionPolicy Bypass -Command "& cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate 2>&1 | ForEach-Object { $s=$_.ToString(); if($s -match 'https://[a-z0-9-]+\.trycloudflare\.com' -and -not $u){  $u=$Matches[0];  [IO.File]::WriteAllText((Join-Path $env:USERPROFILE '.zcode\tools\out-url.txt'),$u);  Write-Host '';  Write-Host ' ==================================================' -ForegroundColor Yellow;  Write-Host ('   OUT-URL: ' + $u) -ForegroundColor Yellow;  Write-Host '   (also shown in Settings, click to copy)' -ForegroundColor Yellow;  Write-Host ' ==================================================' -ForegroundColor Yellow }; Write-Host $s }"
    echo 已另开窗口启动出门隧道——黄色的 OUT-URL 就是今天的网址。
    echo 没看清也没关系：设置 → 其他 → 出门网址，点一下就复制。
    echo.
)

rem 3 秒后自动打开浏览器
start /b cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:8000"

".venv\Scripts\python.exe" -m uvicorn server:app --host 0.0.0.0 --port 8000
pause
