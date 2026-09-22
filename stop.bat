@echo off
rem Stops Screen Solver (only the pythonw process running this app.py).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*screen-solver*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
