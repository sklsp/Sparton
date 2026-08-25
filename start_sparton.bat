@echo off
rem Simple launcher for SPARTON on Windows.
cd /d "%~dp0"

set PY=Apollo\.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo Starting SPARTON with %PY% ...
"%PY%" start_sparton.py %*
