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

echo Checking preview dependencies...
"%RUNNER_EXE%" %RUNNER_ARG% -c "import dotenv, httpx, tzdata" >nul 2>nul
if errorlevel 1 (
    echo Installing missing dependencies into the selected Python environment...
    "%RUNNER_EXE%" %RUNNER_ARG% -m pip install -e .
    if errorlevel 1 (
        echo.
        echo Failed to install preview dependencies.
        pause
        exit /b 1
    )
    echo.
)

echo Generating today's live digest preview...
echo This can take around 30-90 seconds when live feeds and Gemini are enabled.
"%RUNNER_EXE%" %RUNNER_ARG% -m market_digest_bot.html_preview
if errorlevel 1 (
    echo.
    echo Could not open the digest preview.
    pause
)
