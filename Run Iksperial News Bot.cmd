@echo off
setlocal

cd /d "%~dp0"
set "PYTHONPATH=%CD%\src"

set "RUNNER_EXE="
set "RUNNER_ARG="

if exist ".venv\Scripts\python.exe" (
    set "RUNNER_EXE=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "RUNNER_EXE=py"
        set "RUNNER_ARG=-3"
    ) else (
        where python >nul 2>nul
        if %errorlevel%==0 (
            set "RUNNER_EXE=python"
        )
    )
)

if not defined RUNNER_EXE (
    echo.
    echo Could not find Python. Please install Python 3 first.
    pause
    exit /b 1
)

echo.
echo Starting Iksperial News Bot...
echo Leave this window open while testing Discord commands.
echo.

for /f %%A in ('powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '-m market_digest_bot($| )' }; @($p).Count"') do set "BOT_COUNT=%%A"
if not "%BOT_COUNT%"=="" if not "%BOT_COUNT%"=="0" (
    echo Another Iks News bot process is already running.
    echo Close the existing bot window first, then start this launcher again.
    echo.
    pause
    exit /b 1
)

echo Checking bot dependencies...
"%RUNNER_EXE%" %RUNNER_ARG% -c "import discord, dotenv, httpx, tzdata" >nul 2>nul
if errorlevel 1 (
    echo Installing missing dependencies into the selected Python environment...
    "%RUNNER_EXE%" %RUNNER_ARG% -m pip install -e .
    if errorlevel 1 (
        echo.
        echo Failed to install bot dependencies.
        pause
        exit /b 1
    )
    echo.
)

"%RUNNER_EXE%" %RUNNER_ARG% -m market_digest_bot

echo.
echo The bot process stopped.
pause
