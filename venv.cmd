
@echo off
set PYTHONIOENCODING=UTF-8
pushd %~dp0
call venv\Scripts\activate.bat
venv\Scripts\python %*
call venv\Scripts\deactivate.bat
