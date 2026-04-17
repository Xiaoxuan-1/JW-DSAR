@echo off
setlocal

set "ROOT_DIR=%~dp0.."
cd /d "%ROOT_DIR%"

set "PYTHON_BIN=%JWDSAR_PYTHON_BIN%"
if "%PYTHON_BIN%"=="" set "PYTHON_BIN=python"

set "PORT=%JWDSAR_PORT%"
if "%PORT%"=="" set "PORT=7860"
set "PORT=%PORT%"

%PYTHON_BIN% app_scheduled.py
