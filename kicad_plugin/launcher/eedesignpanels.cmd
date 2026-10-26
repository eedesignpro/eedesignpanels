@echo off
rem  EEDESIGNPANELS - the shortcut an engineer double-clicks.
rem
rem  THIS FILE IS DELIBERATELY SIMPLE. It does one thing that Python cannot do
rem  for itself - find a Python at all - and hands everything else to
rem  launch_eedesignpanels.py, where the logic can be read and tested.
rem
rem  TWO WAYS TO USE IT, and the shortcut serves both: double-click it and it
rem  asks which board; DROP A BOARD FILE ONTO IT and that board opens straight
rem  away, because Windows passes a dropped file as the first argument.

setlocal enabledelayedexpansion
set "KICAD_PYTHON="

rem  WHERE KICAD IS, asked of Windows rather than written down. No version
rem  number and no install folder appears in this file, so a KiCad upgrade needs
rem  no edit here. The uninstall entries are the supported list of what is
rem  installed; measured, this search costs about 70 ms. It takes whichever KiCad it meets first and
rem  that is fine - the launcher asks for the newest one and moves to it.
for %%H in (HKLM HKCU) do (
  for /f "delims=" %%K in ('reg query "%%H\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" /f "KiCad" /k /s 2^>nul ^| findstr /i "\\KiCad"') do (
    for /f "tokens=2,*" %%A in ('reg query "%%K" /v InstallLocation 2^>nul ^| findstr /i "InstallLocation"') do (
      if exist "%%B\bin\python.exe" set "KICAD_PYTHON=%%B\bin\python.exe"
    )
  )
)

if not defined KICAD_PYTHON (
  echo.
  echo The panel builder needs KiCad, and Windows does not list KiCad as
  echo installed on this computer.
  echo.
  echo Install KiCad 10 and run this again.
  echo.
  pause
  exit /b 1
)

rem  UTF-8 last, after the registry reading. It is what lets
rem  a board path holding an accented character print instead of stopping the
rem  run, and it is set here rather than earlier because changing the code page
rem  mid-command changes how the lines above are read back.
chcp 65001 >nul 2>&1

"%KICAD_PYTHON%" "%~dp0launch_eedesignpanels.py" %*
set "EEDESIGNPANELS_CODE=%ERRORLEVEL%"

rem  THE WINDOW STAYS OPEN ONLY ON A FAILURE, so the failure message can be
rem  read. A successful run closes it, with no window to dismiss.
if not "%EEDESIGNPANELS_CODE%"=="0" (
  echo.
  pause
)
exit /b %EEDESIGNPANELS_CODE%
