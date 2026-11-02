@echo off
title Material Usage Calculator - keep this window open
cd /d %~dp0
where python >nul 2>nul || (echo Python is not installed / not on PATH & pause & exit /b 1)
python -c "import fitz, OCP, openpyxl, numpy" 2>nul || (echo Installing requirements... & pip install -r requirements.txt)
echo Starting at http://127.0.0.1:8765  (close this window to stop)
python app.py
echo.
echo The server stopped. See the message above.
pause

