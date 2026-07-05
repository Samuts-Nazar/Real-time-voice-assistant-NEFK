@echo off
REM Start the Voice Assistant Server

cd /d "%~dp0"
call venv\Scripts\activate
python -m server.main
pause