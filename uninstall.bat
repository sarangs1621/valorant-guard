@echo off
REM Removes Valorant Guard completely: autostart, keepalive task, shortcuts, files.
set "DEST=%LOCALAPPDATA%\ValorantGuard"

if exist "%DEST%\ValorantGuard.exe" (
    start "" /wait "%DEST%\ValorantGuard.exe" --uninstall
)
taskkill /F /IM ValorantGuard.exe >nul 2>&1
schtasks /Delete /F /TN "Valorant Guard Keepalive" >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ValorantGuard /f >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Valorant Guard.lnk" >nul 2>&1
rmdir /S /Q "%DEST%" >nul 2>&1

echo Valorant Guard has been removed.
echo Use Riot's own VALORANT shortcut to play again.
pause
