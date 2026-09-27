@echo off
chcp 65001 >nul
title MUST VPN Terminal
for %%I in ("%~dp0..\MUST-VPN-VM") do set "MUST_VM_HOME=%%~fI"
if not exist "%MUST_VM_HOME%\config.json" (
  echo Configured VM state was not found at "%MUST_VM_HOME%".
  echo Please copy this path when reporting the error.
  pause
  exit /b 1
)
"%~dp0must-vm.exe" terminal
set "MUST_VM_EXIT=%ERRORLEVEL%"
if not "%MUST_VM_EXIT%"=="0" (
  echo.
  echo MUST VPN Terminal exited with code %MUST_VM_EXIT%.
  echo Please copy the error shown above and press any key to close this window.
  pause >nul
)
exit /b %MUST_VM_EXIT%
