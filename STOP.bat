@echo off
setlocal EnableDelayedExpansion
title Grand Park Auto - Stop

echo.
echo  Stopping Grand Park Auto...
echo.

set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"TCP.*:8080 .*LISTENING"') do set "FOUND=%%P"
if defined FOUND (
  echo   dispatcher on PID !FOUND! - stopping
  taskkill /f /pid !FOUND! >nul 2>&1
) else (
  echo   dispatcher not running
)

tasklist /fi "imagename eq ParkingSimulator.exe" 2>nul | findstr /i /c:"ParkingSimulator.exe" >nul
if not errorlevel 1 (
  echo   simulator - stopping
  taskkill /f /im ParkingSimulator.exe >nul 2>&1
) else (
  echo   simulator not running
)

echo.
echo  Stopped.
ping -n 3 127.0.0.1 >nul 2>&1
exit /b 0
