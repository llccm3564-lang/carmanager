@echo off
title CarManager Auto Update

set "DIR=%~dp0"
set "TEMP_ZIP=%TEMP%\cm_update.zip"
set "TEMP_DIR=%TEMP%\cm_update"

echo [1/4] Downloading latest version...
powershell -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/llccm3564-lang/carmanager/archive/refs/heads/master.zip' -OutFile '%TEMP_ZIP%' -UseBasicParsing } catch { Write-Host 'DOWNLOAD FAILED'; exit 1 }"
if errorlevel 1 (
    echo Download failed. Check internet connection.
    pause
    exit /b 1
)

echo [2/4] Extracting update...
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
powershell -Command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_DIR%' -Force"

echo [3/4] Applying update...
if exist "%DIR%.env" copy "%DIR%.env" "%TEMP%\cm_env_backup" >nul 2>&1
xcopy "%TEMP_DIR%\carmanager-master\*" "%DIR%" /s /y /q >nul 2>&1
if exist "%TEMP%\cm_env_backup" (
    copy "%TEMP%\cm_env_backup" "%DIR%.env" >nul 2>&1
    del "%TEMP%\cm_env_backup" >nul 2>&1
)

echo [4/4] Installing packages and running...
py -m pip install -r "%DIR%requirements.txt" -q 2>nul
py "%DIR%carmanager.py" --headless
pause
