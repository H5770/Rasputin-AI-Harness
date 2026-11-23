@echo off
cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"

rem pythonw = no console window. To see errors/debug, change pythonw to python and add "pause" at the end.
start "" pythonw Python\desktop.py
exit