@echo off
chcp 65001 >nul
setlocal
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name 3DModelScope --icon app_icon.ico --add-data "app_icon.ico;." app.py
if errorlevel 1 (
    echo.
    echo A build sikertelen volt.
    pause
    exit /b 1
)
echo.
echo Elkészült: dist\3DModelScope.exe
echo Az adatbazis az EXE melletti 3DModelScope.db fajl lesz.

set "DEPLOY_TARGET=C:\Users\ÁrpádNagykékesi\AppData\Local\Bromium\3DModelScope.exe"
copy /Y "dist\3DModelScope.exe" "%DEPLOY_TARGET%"
if errorlevel 1 (
    echo.
    echo Nem sikerult a masolas ide: %DEPLOY_TARGET%
    pause
    exit /b 1
)
echo Atmasolva ide: %DEPLOY_TARGET%
pause
