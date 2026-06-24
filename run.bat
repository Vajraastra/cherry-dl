@echo off
REM cherry-dl launcher (Windows) — wrapper sin lógica.
REM Delega todo a run.sh ejecutado con el Git Bash que viene con Git para Windows.
"%ProgramFiles%\Git\bin\bash.exe" "%~dp0run.sh" %*
exit /b %errorlevel%
