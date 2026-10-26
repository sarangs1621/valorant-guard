@echo off
REM Builds Valorant Guard, replaces your VALORANT shortcuts, and starts the
REM background guard with Windows. Run it again any time you edit the .py.
cd /d "%~dp0"

echo Installing PyInstaller, pystray and Pillow...
python -m pip install --quiet pyinstaller pystray pillow || goto :error

echo Building ValorantGuard.exe...
python -m PyInstaller --onefile --noconsole --hidden-import pystray._win32 --name ValorantGuard valorant_guard.py || goto :error

set "DEST=%LOCALAPPDATA%\ValorantGuard"
if not exist "%DEST%" mkdir "%DEST%"

taskkill /F /IM ValorantGuard.exe >nul 2>&1
REM Remove the first version's startup copy if it exists
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ValorantGuard.exe" >nul 2>&1

copy /Y "dist\ValorantGuard.exe" "%DEST%\ValorantGuard.exe" >nul || goto :error

echo Creating shortcuts...
start "" /wait "%DEST%\ValorantGuard.exe" --install
if errorlevel 1 goto :error

start "" "%DEST%\ValorantGuard.exe"

echo.
echo Done.
echo  - Your desktop and Start menu now have a VALORANT shortcut that shows
echo    the warning before the Riot Client opens.
echo  - The background guard is running. It starts by itself every time you
echo    log in to Windows, and restarts itself within 5 minutes if it is ever
echo    closed. You do not need to run this file again.
echo  - It lives in the system tray (the ^^ arrow near the clock). Right-click
echo    the red V icon to preview the warning or Quit. Quit keeps it off
echo    until your next Windows login.
echo  - If you still see Riot's original VALORANT shortcut (on the desktop or
echo    pinned to the taskbar), delete or unpin it and use the new one.
echo.
echo To remove it completely, run uninstall.bat.
pause
exit /b 0

:error
echo.
echo Something went wrong. Make sure Python is installed and on PATH.
pause
exit /b 1
