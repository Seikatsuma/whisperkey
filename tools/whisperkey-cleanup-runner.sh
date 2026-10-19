#!/bin/bash
# WhisperKey cleanup runner — удаляет аудиозаписи старше 24 часов и пишет журнал.
# Этот скрипт запускается LaunchAgent; его не нужно запускать руками.

set -euo pipefail

RECORDINGS_DIR="${1:-$HOME/Library/Application Support/WhisperKey/Recordings}"
LOG_DIR="$HOME/Library/Logs"
LOG_FILE="$LOG_DIR/whisperkey-cleanup.log"

mkdir -p "$LOG_DIR"

if [ ! -d "$RECORDINGS_DIR" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Папка записей не найдена: $RECORDINGS_DIR" >> "$LOG_FILE"
    exit 0
fi

# -maxdepth 1: не выходим за пределы выделенной папки.
# -mmin +1440: строго старше 24 часов (1440 минут).
# -print печатает удаляемые файлы, -delete удаляет их.
{
    echo "$(date '+%Y-%m-%d %H:%M:%S') Проверка: $RECORDINGS_DIR"
    /usr/bin/find "$RECORDINGS_DIR" \
        -maxdepth 1 \
        -type f \
        \( \
            -name "*.m4a" -o \
            -name "*.wav" -o \
            -name "*.caf" -o \
            -name "*.aac" -o \
            -name "*.mp3" \
        \) \
        -mmin +1440 \
        -print -delete
    echo "$(date '+%Y-%m-%d %H:%M:%S') Готово"
} >> "$LOG_FILE" 2>&1
