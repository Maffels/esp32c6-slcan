@echo off
setlocal
set "ROOT=%~dp0"
if /I "%~1"=="--cli" (
  if "%~2"=="" (
    python "%ROOT%monitor_example.py" COM8
  ) else (
    python "%ROOT%monitor_example.py" %2 %3 %4 %5
  )
) else (
  python "%ROOT%slcan_monitor_gui.py" %*
)
endlocal
