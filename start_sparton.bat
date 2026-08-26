@echo off
rem Simple launcher for SPARTON on Windows.
cd /d "%~dp0"

set PY=Apollo\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo Starting SPARTON with %PY% ...
"%PY%" start_sparton.py %*

rem Double-clicked from Explorer the console closes the instant this exits,
rem taking any error message with it. Hold the window open so it can be read.
echo.
echo SPARTON exited with code %ERRORLEVEL%.
pause
