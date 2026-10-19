#!/bin/bash
# Установщик LaunchAgent для очистки записей WhisperKey старше 24 часов.
# Запускай на Mac ТОЛЬКО после того, как убедился, что записи сохраняются в нужную папку.
#
# Использование:
#   bash tools/whisperkey-recording-cleanup.sh
# или с другой папкой:
#   bash tools/whisperkey-recording-cleanup.sh "$HOME/Custom/Recordings"

set -euo pipefail

RECORDINGS_DIR="${1:-$HOME/Library/Application Support/WhisperKey/Recordings}"
LOG_DIR="$HOME/Library/Logs"
LOG_FILE="$LOG_DIR/whisperkey-cleanup.log"
PLIST_LABEL="com.seikatsuma.whisperkey.recording-cleanup"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

# Простая проверка: если папки ещё нет, предупреждаем, но не ломаемся.
if [ ! -d "$RECORDINGS_DIR" ]; then
    echo "Папка записей пока не существует: $RECORDINGS_DIR"
    echo "Создаю её. Если хотите другую папку — передайте путь аргументом."
    mkdir -p "$RECORDINGS_DIR"
fi

# Абсолютный путь для plist (LaunchAgent не любит тильды и переменные).
ABS_RECORDINGS_DIR="$(cd "$RECORDINGS_DIR" && pwd -P)"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/find</string>
        <string>${ABS_RECORDINGS_DIR}</string>
        <string>-maxdepth</string>
        <string>1</string>
        <string>-type</string>
        <string>f</string>
        <string>\(</string>
        <string>-name</string>
        <string>*.m4a</string>
        <string>-o</string>
        <string>-name</string>
        <string>*.wav</string>
        <string>-o</string>
        <string>-name</string>
        <string>*.caf</string>
        <string>-o</string>
        <string>-name</string>
        <string>*.aac</string>
        <string>-o</string>
        <string>-name</string>
        <string>*.mp3</string>
        <string>\)</string>
        <string>-mmin</string>
        <string>+1440</string>
        <string>-delete</string>
    </array>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
EOF

# Регистрируем агента. Если он уже есть — перезагружаем.
if launchctl list "$PLIST_LABEL" >/dev/null 2>&1; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi
launchctl load "$PLIST_PATH"
launchctl start "$PLIST_LABEL"

echo "LaunchAgent установлен: $PLIST_PATH"
echo "Папка очистки: $ABS_RECORDINGS_DIR"
echo "Журнал: $LOG_FILE"
echo "Первый запрос сделан. Проверить состояние: launchctl list | grep $PLIST_LABEL"
