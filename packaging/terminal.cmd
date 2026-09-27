@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title MUST VPN Terminal
"%~dp0must-vm.exe" terminal
set "MUST_VM_EXIT=%ERRORLEVEL%"
if not "%MUST_VM_EXIT%"=="0" (
  echo.
  echo MUST VPN Terminal exited with code %MUST_VM_EXIT%.
  echo Please copy the error shown above and press any key to close this window.
  pause >nul
)
exit /b %MUST_VM_EXIT%
