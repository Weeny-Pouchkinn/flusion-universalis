@echo off
cd /d "%~dp0"
py culture_painter.py
if errorlevel 1 pause
