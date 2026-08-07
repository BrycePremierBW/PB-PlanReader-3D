@echo off
setlocal
cd /d %~dp0

echo Installing / checking TradeReader requirements...
py -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 python -m pip install -r requirements.txt

if not defined PLANREADER_DATA_DIR set PLANREADER_DATA_DIR=%CD%\tradereader_data

echo.
echo Starting TradeReader 3D...
py -m streamlit run tradereader_app.py
if %ERRORLEVEL% NEQ 0 python -m streamlit run tradereader_app.py
pause
