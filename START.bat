@echo off
setlocal EnableDelayedExpansion
title Grand Park Auto - Dispatcher Launcher
pushd "%~dp0"

REM Deliberately avoids `find` and `timeout`: git-bash and similar shells put
REM Unix versions of both ahead of the Windows ones on PATH, which breaks this
REM script in ways that are painful to debug. findstr and ping have no such
REM collision, so the launcher behaves the same however it is invoked.

echo.
echo  ===============================================
echo   Grand Park Auto  -  Team Mix_and_Match
echo  ===============================================
echo.

set "SIM_EXE=ParkingSimulator-win-x64\ParkingSimulator-win-x64\ParkingSimulator.exe"
set "VENV_PY=.venv\Scripts\python.exe"
set "SETTINGS=ParkingSimulator-win-x64\ParkingSimulator-win-x64\settings\settings.json"
REM The split-screen operator dashboard. "/" is the older HUD, still served.
set "DASH_URL=http://127.0.0.1:8080/dashboard"
set "HUD_URL=http://127.0.0.1:8080/"

if not exist "%SIM_EXE%" (
  echo  [X] Simulator not found:
  echo      %SIM_EXE%
  echo.
  echo      Extract ParkingSimulator-win-x64.zip into the project folder.
  goto :fail
)

REM ------------------------------------------------- python / dependencies
if not exist "%VENV_PY%" (
  echo  [1/5] No virtual environment - creating one ^(first run only, ~1 min^)
  python -m venv .venv
  if errorlevel 1 (
    echo  [X] Could not create the virtual environment. Is Python installed?
    goto :fail
  )
  echo        installing dependencies...
  "%VENV_PY%" -m pip install -q --upgrade pip
  "%VENV_PY%" -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo  [X] Dependency install failed.
    goto :fail
  )
) else (
  echo  [1/5] Virtual environment found.
)

if not exist ".env" (
  echo        no .env found - copying .env.example
  copy /y ".env.example" ".env" >nul
)

REM ------------------------------------------------- clear a stale listener
echo  [2/5] Checking port 8080...
set "STALE="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"TCP.*:8080 .*LISTENING"') do set "STALE=%%P"
if defined STALE (
  echo        stale dispatcher on PID !STALE! - stopping it
  taskkill /f /pid !STALE! >nul 2>&1
  call :sleep 2
) else (
  echo        port is free.
)

REM ------------------------------------------------------- start simulator
echo  [3/5] Starting the simulator...
tasklist /fi "imagename eq ParkingSimulator.exe" 2>nul | findstr /i /c:"ParkingSimulator.exe" >nul
if not errorlevel 1 (
  echo        already running - leaving it alone.
) else (
  start "Grand Park Auto Simulator" "%SIM_EXE%"
)

echo        waiting for the REST API on :9898 ...
set /a TRIES=0
:waitsim
set /a TRIES+=1
REM Any HTTP response means it is alive. A 401 here is expected - that
REM endpoint wants a bearer token, and we only care that something answered.
curl -s -o nul -m 2 http://127.0.0.1:9898/api/v1/test
if not errorlevel 1 goto :simup
if !TRIES! GEQ 30 (
  echo.
  echo  [i] Simulator did not answer on :9898 after ~60s.
  echo      Starting the dispatcher anyway - it can sync later via
  echo      POST http://127.0.0.1:8080/api/manual/sync
  goto :startapp
)
call :sleep 2
goto :waitsim

:simup
echo        simulator is up.

findstr /c:"127.0.0.1:8080/webhooks/simulator" "%SETTINGS%" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [i] settings.json WebhookUrl does not point at this dispatcher.
  echo      Set it to:  http://127.0.0.1:8080/webhooks/simulator
  echo      Without that no webhooks arrive and nothing will happen.
  echo.
)

REM ------------------------------------------------------ start dispatcher
:startapp
echo  [4/5] Starting the dispatcher on :8080 ...
start "Grand Park Auto Dispatcher" "%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8080

echo        waiting for it to come up ...
set /a TRIES=0
:waitapp
set /a TRIES+=1
curl -s -o nul -m 2 http://127.0.0.1:8080/healthz
if not errorlevel 1 goto :appup
if !TRIES! GEQ 25 (
  echo.
  echo  [X] Dispatcher did not start. Check its window for the error.
  goto :fail
)
call :sleep 1
goto :waitapp

:appup
echo        dispatcher is up.

echo  [5/5] Opening the operator dashboard...
start "" "%DASH_URL%"

echo.
echo  ===============================================
echo   Running.
echo.
echo   Dashboard    %DASH_URL%    ^<- split-screen operator view
echo   Operator HUD %HUD_URL%
echo   Gate portal  http://127.0.0.1:8080/gate
echo   Health       http://127.0.0.1:8080/healthz
echo.

REM Surface the setting that most often causes confusion.
set "HEALTH=%TEMP%\gpa_health.json"
curl -s -m 3 -o "%HEALTH%" http://127.0.0.1:8080/healthz 2>nul
findstr /c:"\"autopilot\":true" "%HEALTH%" >nul 2>&1
if not errorlevel 1 (
  echo   AUTOPILOT IS ON - commanding the real simulator.
) else (
  echo   AUTOPILOT is OFF - decisions logged as [dry-run] only.
  echo   Set AUTOPILOT=true in .env and rerun to go live.
)
if exist "%HEALTH%" del "%HEALTH%" >nul 2>&1

echo.
echo   Two windows opened: simulator and dispatcher.
echo   Run STOP.bat to shut both down.
echo  ===============================================
echo.

popd
echo  Press any key to close this launcher ^(the app keeps running^).
pause >nul
exit /b 0

REM ----------------------------------------------------------------- utils
:sleep
REM ping-based sleep: `timeout` refuses to run when stdin is redirected,
REM which happens whenever this is launched from a script rather than a click.
set /a _P=%~1+1
ping -n %_P% 127.0.0.1 >nul 2>&1
exit /b 0

:fail
echo.
popd
echo  Press any key to close.
pause >nul
exit /b 1
