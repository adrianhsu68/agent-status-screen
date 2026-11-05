@echo off
rem Double-click to show the status page on the small screen; -Watch / -Stop: see screen.ps1
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0screen.ps1" %*
