@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .venv call .venv\Scripts\activate.bat
python -m pytest tests -v -p no:cacheprovider -W ignore::DeprecationWarning
pause
