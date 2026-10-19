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
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.seikatsuma.whisperkey.recording-cleanup"
PLIST_PATH="$LAUNCH_AGENTS_DIR/${PLIST_LABEL}.plist"

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$TOOLS_DIR/whisperkey-cleanup-runner.sh"

if [ ! -x "$RUNNER" ]; then
    echo "Не найден runner: $RUNNER"
    echo "Убедитесь, что скрипт tools/whisperkey-cleanup-runner.sh лежит рядом и исполняемый."
    exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR"

# Простая проверка: если папки ещё нет, предупреждаем, но не ломаемся.
if [ ! -d "$RECORDINGS_DIR" ]; then
    echo "Папка записей пока не существует: $RECORDINGS_DIR"
    echo "Создаю её. Если хотите другую папку — передайте путь аргументом."
    mkdir -p "$RECORDINGS_DIR"
fi

# Абсолютный путь для plist (LaunchAgent не любит тильды и переменные).
ABS_RECORDINGS_DIR="$(cd "$RECORDINGS_DIR" && pwd -P)"
ABS_RUNNER="$(cd "$TOOLS_DIR" && pwd -P)/whisperkey-cleanup-runner.sh"

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
        <string>${ABS_RUNNER}</string>
        <string>${ABS_RECORDINGS_DIR}</string>
    </array>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/whisperkey-cleanup.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/whisperkey-cleanup.log</string>
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
echo "Runner: $ABS_RUNNER"
echo "Журнал: $HOME/Library/Logs/whisperkey-cleanup.log"
echo "Первый запрос сделан. Проверить состояние: launchctl list | grep $PLIST_LABEL"
