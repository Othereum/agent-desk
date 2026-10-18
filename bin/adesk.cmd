@echo off
rem agent-desk CLI for PowerShell and cmd (bin/adesk is the sh version for the Bash tool).
rem Skips Windows app execution aliases (...\WindowsApps\python.exe) unless nothing else is found.
setlocal
set "py="
set "fallback="
for %%n in (python3 python) do for /f "delims=" %%p in ('where %%n 2^>nul') do call :pick "%%p"
if not defined py set "py=%fallback%"
if not defined py (
  1>&2 echo adesk: Python 3.11+ not found on PATH
  exit /b 127
)
"%py%" "%~dp0desk.py" %*
exit /b %errorlevel%

:pick
if defined py exit /b
echo %~1| findstr /i "\\WindowsApps\\" >nul && (if not defined fallback set "fallback=%~1") || set "py=%~1"
exit /b
