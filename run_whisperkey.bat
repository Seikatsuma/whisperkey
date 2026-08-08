@echo off
:: Файл в UTF-8, а cmd.exe стартует в cp866: без chcp все русские строки ниже
:: выводятся кракозябрами — включая инструкцию про .env, которую надо прочитать.
:: Файл держать в UTF-8 БЕЗ BOM: с BOM cmd ломает разбор первой команды.
chcp 65001 >nul
:: Кодировка вывода самого Python. Уведомления о деградации (ушли на локальную
:: модель, потерян кусок, вырезан водяной знак) приходят через print — если они
:: нечитаемы или падают с UnicodeEncodeError, вся диагностика обесценивается.
set "PYTHONIOENCODING=utf-8"
title WhisperKey Launcher
cd /d "%~dp0"

echo WhisperKey — запуск из папки проекта...
echo.

:: ── Самообновление ──────────────────────────────────────────────────────────
:: Обновление приезжает при каждом запуске, чтобы не переносить папку руками.
:: --ff-only: если в папке есть свои правки, обновление не применится вместо
:: того, чтобы их затереть. Любая неудача — запускаемся на том, что уже лежит.
:: Отключить: создать рядом файл .no-update
if exist ".no-update" goto :skip_update
if not exist ".git" (
    echo Папка не подключена к репозиторию — обновляться неоткуда.
    echo Один раз выполните, и дальше всё будет само:
    echo    git clone https://github.com/Seikatsuma/whisperkey.git WhisperKey
    echo    и перенесите в новую папку файл .env со своим ключом.
    echo.
    goto :skip_update
)
git --version >nul 2>&1
if %errorlevel% neq 0 goto :skip_update
echo Проверяю обновление...
set "GIT_TERMINAL_PROMPT=0"
set "GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new"
git pull --ff-only --quiet 2>"%TEMP%\whisperkey_update.log"
if %errorlevel% equ 0 (
    echo Версия актуальна.
) else (
    echo Обновиться не вышло — работаю на текущей версии.
    type "%TEMP%\whisperkey_update.log" 2>nul
)
echo.
:skip_update

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
    echo WhisperKey завершился. Причина — в сообщениях выше.
    echo Если текст не вставляется — попробуйте: ПКМ по этому файлу -^> "Запуск от имени администратора"
    pause
)
