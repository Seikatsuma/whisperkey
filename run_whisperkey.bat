@echo off
title WhisperKey Launcher
echo Starting WhisperKey...

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not found. Please install Python 3.10+ and add it to PATH.
    pause
    exit /b
)

:: Check for .env file
if not exist .env (
    echo Warning: .env file not found!
    echo Please copy .env.example to .env and add your GROQ_API_KEY.
    pause
)

:: Run the script
python whisperkey.py
if %errorlevel% neq 0 (
    echo.
    echo WhisperKey stopped with an error.
    pause
)
