@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not on PATH.
    pause
    exit /b 1
)
python -B "%~dp0build\build_installer.py" %*
set "BUILD_RESULT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %BUILD_RESULT%
