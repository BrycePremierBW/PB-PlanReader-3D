@echo off
title Premier Brushworks Plan Reader and 3D Take-off
cd /d "%~dp0"

echo.
echo Premier Brushworks Plan Reader and 3D Take-off
echo =================================================
echo.
echo Installing or updating requirements...
py -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 python -m pip install -r requirements.txt

echo.
echo Starting PlanReader...
py -m streamlit run pb_planreader_3d_app.py
if %ERRORLEVEL% NEQ 0 python -m streamlit run pb_planreader_3d_app.py
pause
