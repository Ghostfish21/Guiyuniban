@echo off
chcp 936 >nul
set PYTHONUTF8=1
python -X utf8 "%~dp0guiyuniban_control.py" %*