@echo off
title BussanCar Auto Loop

:LOOP
set "DIR=%~dp0"
set "TEMP_ZIP=%TEMP%\cm_update.zip"
set "TEMP_DIR=%TEMP%\cm_update"

echo ========================================
echo [1/4] Downloading latest version...
powershell -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/llccm3564-lang/carmanager/archive/refs/heads/master.zip' -OutFile '%TEMP_ZIP%' -UseBasicParsing } catch { Write-Host 'DOWNLOAD FAILED'; exit 1 }"
if errorlevel 1 (
    echo Download failed. Retrying in 5 minutes...
    timeout /t 300 /nobreak >nul
    goto LOOP
)

echo [2/4] Extracting update...
if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
powershell -Command "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_DIR%' -Force"

echo [3/4] Applying update...
xcopy "%TEMP_DIR%\carmanager-master\*" "%DIR%" /s /y /q >nul 2>&1

echo [4/4] Installing packages and running...
py -m pip install -r "%DIR%requirements.txt" -q 2>nul
py "%DIR%bussancar.py"

echo.
echo Run complete. Next run in 5 minutes...
echo (Press Ctrl+C to stop)
timeout /t 300 /nobreak >nul
goto LOOP
