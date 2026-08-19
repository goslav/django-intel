@echo off
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
"%~dp0.venv\Scripts\python.exe" manage.py refresh_active_dealers >> "%~dp0logs\dealer_snapshots.log" 2>&1
exit /b %errorlevel%
