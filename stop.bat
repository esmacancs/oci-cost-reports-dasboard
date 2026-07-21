@echo off
echo Stopping OCI Cost Dashboard...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *app.py*" >nul 2>&1
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" ^| findstr python') do (
    wmic process where "ProcessId=%%a" get CommandLine 2>nul | findstr /i "app.py" >nul && taskkill /F /PID %%a >nul 2>&1
)
echo Done.
