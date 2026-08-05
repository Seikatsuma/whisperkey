#!/bin/bash
# Открывает НОВОЕ окно Терминала и запускает в нём WhisperKey.
#
# Отличие от «Запустить WhisperKey.command»: тот рассчитан на двойной клик,
# когда Терминал открывается сам. Этот — для запуска откуда угодно
# (Automator, Shortcuts, Raycast, Alfred, строка команд), где окна нет
# и его надо создать явно.

set -u

# ─── 1. Находим папку проекта ────────────────────────────────────────────────
cd "$(dirname "$0")" || exit 1
CONFIG="$HOME/.whisperkey_path"

if [ ! -f "whisperkey.py" ] && [ -f "$CONFIG" ]; then
  SAVED="$(cat "$CONFIG")"
  [ -f "$SAVED/whisperkey.py" ] && cd "$SAVED" || true
fi

if [ ! -f "whisperkey.py" ]; then
  for guess in \
    "$HOME/Desktop/Whisper на MAC" \
    "$HOME/Desktop/whisperkey" \
    "$HOME/whisperkey/Whisper на MAC" \
    "$HOME/Documents/whisperkey"; do
    if [ -f "$guess/whisperkey.py" ]; then cd "$guess" || exit 1; break; fi
  done
fi

if [ ! -f "whisperkey.py" ]; then
  osascript -e 'display alert "WhisperKey" message "Не нашёл папку с whisperkey.py. Запусти один раз «Запустить WhisperKey.command» из папки проекта — путь запомнится." as critical'
  exit 1
fi

PROJECT_DIR="$(pwd)"
echo "$PROJECT_DIR" > "$CONFIG"

# ─── 2. Собираем команду для новой сессии ────────────────────────────────────
# venv подхватывается, если есть: без него не найдутся зависимости.
if [ -f "$PROJECT_DIR/venv/bin/python3" ]; then
  PY="$PROJECT_DIR/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  osascript -e 'display alert "WhisperKey" message "Python 3 не найден. Установи Python 3.10 или новее." as critical'
  exit 1
fi

# ─── 3. Экранируем для AppleScript ───────────────────────────────────────────
# В пути бывают пробелы и кириллица («Whisper на MAC»), а do script принимает
# строку AppleScript: обратные слэши и кавычки надо удвоить, иначе команда
# развалится на первом же пробеле.
escape_applescript() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

DIR_ESC="$(escape_applescript "$PROJECT_DIR")"
PY_ESC="$(escape_applescript "$PY")"

# Одинарные кавычки внутри shell-команды для do script:
# cd в папку, экспорт переменных для Intel Mac, запуск.
SHELL_CMD="cd \\\"$DIR_ESC\\\" && export KMP_DUPLICATE_LIB_OK=TRUE OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 && clear && \\\"$PY_ESC\\\" whisperkey.py"

# ─── 4. Открываем Терминал и запускаем ───────────────────────────────────────
osascript <<EOF
tell application "Terminal"
    activate
    do script "$SHELL_CMD"
end tell
EOF
