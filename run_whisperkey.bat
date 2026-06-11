@echo off
title WhisperKey Launcher
cd /d "%~dp0"

echo WhisperKey — запуск из папки проекта...
echo.

:: Prefer venv Python if Cursor/AI created a virtual environment
set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

:: Check Python
"%PY%" --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден.
    echo Установите Python 3.10+ с python.org и поставьте галочку "Add Python to PATH".
    echo Либо создайте venv: python -m venv venv
    pause
    exit /b 1
)

for /f "delims=" %%v in ('"%PY%" --version 2^>^&1') do echo Python: %%v

:: Check .env
if not exist ".env" (
    echo.
    echo [ВНИМАНИЕ] Файл .env не найден!
    echo Скопируйте:  copy .env.example .env
    echo Затем откройте .env и вставьте GROQ_API_KEY с https://console.groq.com/keys
    echo Без ключа будет только медленный offline-режим.
    echo.
    pause
)

echo Starting WhisperKey...
"%PY%" -u whisperkey_win.py
if %errorlevel% neq 0 (
    echo.
    echo WhisperKey остановлен с ошибкой.
    echo Если текст не вставляется — попробуйте: ПКМ по этому файлу -^> "Запуск от имени администратора"
    pause
)
