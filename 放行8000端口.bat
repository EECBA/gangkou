@echo off
echo SoulHome: opening firewall port 8000 ...
netsh advfirewall firewall delete rule name="SoulHome 8000" >nul 2>&1
netsh advfirewall firewall add rule name="SoulHome 8000" dir=in action=allow protocol=TCP localport=8000
echo.
echo Done. You can close this window.
pause
