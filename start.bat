@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=*
set PYTHONIOENCODING=utf-8
set PORT=8765

echo.
echo   ==========================================
echo     Exam Answer Assistant
echo     http://127.0.0.1:%PORT%
echo   ==========================================
echo.
echo   Manual steps: pass CAPTCHA / click Submit
echo   Everything else is automated.
echo.

if not exist "src\session.json" (
  if exist "data\session.example.json" (
    copy /y "data\session.example.json" "src\session.json" >nul
    echo   [i] Created src\session.json - fill in your credentials first.
  )
)

python "%~dp0src\app.py"

endlocal
pause
